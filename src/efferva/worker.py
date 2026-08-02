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
from efferva.codex_session import CodexSession
from efferva.codex_transport import CodexTransport
from efferva.codex_tunnel import RedisCodexTunnel
from efferva.config import Settings, get_settings
from efferva.sandbox import SandboxProvider
from efferva.sandbox.manager import create_sandbox_control_plane

LOGGER = logging.getLogger(__name__)

WORKER_READY = Gauge(
    "efferva_worker_ready",
    "Whether this worker can accept Codex Sessions",
)
ACTIVE_CODEX_SESSIONS = Gauge(
    "efferva_worker_active_codex_sessions",
    "Active Codex Sessions held by this worker",
)
CODEX_SESSION_CAPACITY = Gauge(
    "efferva_worker_codex_session_capacity",
    "Maximum Codex Sessions accepted by this worker",
)


class CodexSessionWorker:
    """Claims Session channels and keeps one Codex WebSocket per Session."""

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
        self._sessions: dict[str, asyncio.Task[None]] = {}
        self._stopping = asyncio.Event()

    async def run(self) -> None:
        CODEX_SESSION_CAPACITY.set(self._settings.worker_session_capacity)
        await self._tunnel.ping()
        WORKER_READY.set(1)

        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(name, self._stopping.set)

        try:
            while not self._stopping.is_set():
                self._remove_finished_sessions()
                capacity = (
                    self._settings.worker_session_capacity - len(self._sessions)
                )
                if capacity <= 0:
                    await self._wait_for_capacity()
                    continue

                claimed = await self._tunnel.reclaim_stale_channels(
                    self._worker_id,
                    min_idle_ms=(
                        self._settings.worker_session_claim_idle_seconds * 1000
                    ),
                    count=capacity,
                )
                capacity -= len(claimed)
                if capacity > 0:
                    claimed.extend(
                        await self._tunnel.claim_new_channels(
                            self._worker_id,
                            count=capacity,
                        )
                    )
                await self._start_sessions(claimed)
        finally:
            WORKER_READY.set(0)
            self._stopping.set()
            await self._drain_sessions()

    async def _start_sessions(
        self,
        claimed: list[tuple[str, dict[str, Any]]],
    ) -> None:
        for dispatch_id, command in claimed:
            channel_id = str(command.get("channelId") or "")
            session_id = str(command.get("sessionId") or "")
            if not channel_id or not session_id:
                continue
            if channel_id in self._sessions:
                continue
            self._sessions[channel_id] = asyncio.create_task(
                self._serve_session(dispatch_id, command),
                name=f"efferva-codex-session:{session_id}",
            )
        ACTIVE_CODEX_SESSIONS.set(len(self._sessions))

    async def _serve_session(
        self,
        dispatch_id: str,
        command: dict[str, Any],
    ) -> None:
        channel_id = str(command["channelId"])
        session_id = str(command["sessionId"])
        acquired = await self._tunnel.acquire_channel_lease(
            channel_id,
            self._worker_id,
        )
        if not acquired:
            return

        owner = asyncio.current_task()
        lease_task = asyncio.create_task(
            self._renew_lease(session_id, channel_id, dispatch_id, owner)
        )
        status = "closed"
        error: str | None = None
        try:
            state = await self._tunnel.get_state(channel_id)
            if state.get("status") == "ready":
                raise RuntimeError(
                    "the worker holding this Codex Session was lost"
                )
            if state.get("status") in {"closed", "failed"}:
                return
            if not await self._tunnel.active_clients(channel_id):
                return

            await self._tunnel.set_state(
                channel_id,
                {"status": "connecting", "workerId": self._worker_id},
            )
            async with self._transport.connect({"id": session_id}) as upstream:
                codex_session = CodexSession(channel_id, upstream, self._tunnel)
                await codex_session.initialize()
                await self._tunnel.set_state(
                    channel_id,
                    {"status": "ready", "workerId": self._worker_id},
                )
                await codex_session.run()
        except asyncio.CancelledError:
            status = "failed"
            error = "the worker closed this Codex Session"
            raise
        except Exception as exception:
            status = "failed"
            error = str(exception)
        finally:
            lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await lease_task
            await self._finish_session(
                session_id,
                channel_id,
                dispatch_id,
                status=status,
                error=error,
            )

    async def _renew_lease(
        self,
        session_id: str,
        channel_id: str,
        dispatch_id: str,
        owner: asyncio.Task[Any] | None,
    ) -> None:
        while True:
            await asyncio.sleep(self._tunnel.heartbeat_interval_seconds)
            try:
                await self._tunnel.touch_channel_claim(
                    self._worker_id,
                    dispatch_id,
                )
                renewed = await self._tunnel.renew_channel_lease(
                    session_id,
                    channel_id,
                    self._worker_id,
                )
            except Exception:
                renewed = False
            if not renewed:
                if owner is not None:
                    owner.cancel()
                return

    async def _finish_session(
        self,
        session_id: str,
        channel_id: str,
        dispatch_id: str,
        *,
        status: str,
        error: str | None,
    ) -> None:
        state: dict[str, Any] = {"status": status}
        if error:
            state["error"] = error
        await self._tunnel.set_state(channel_id, state)
        clients = await self._tunnel.active_clients(channel_id)
        await asyncio.gather(
            *(
                self._tunnel.send_server_frame(
                    channel_id,
                    client_id,
                    kind="close",
                )
                for client_id in clients
            ),
            return_exceptions=True,
        )
        await self._tunnel.release_channel(
            session_id,
            channel_id,
            self._worker_id,
            dispatch_id,
        )

    def _remove_finished_sessions(self) -> None:
        for channel_id, task in self._sessions.items():
            if task.done() and not task.cancelled():
                try:
                    task.result()
                except Exception:
                    LOGGER.exception("Codex channel %s failed", channel_id)
        self._sessions = {
            channel_id: task
            for channel_id, task in self._sessions.items()
            if not task.done()
        }
        ACTIVE_CODEX_SESSIONS.set(len(self._sessions))

    async def _wait_for_capacity(self) -> None:
        if not self._sessions:
            return
        await asyncio.wait(
            self._sessions.values(),
            timeout=1,
            return_when=asyncio.FIRST_COMPLETED,
        )

    async def _drain_sessions(self) -> None:
        if not self._sessions:
            return
        _, pending = await asyncio.wait(
            self._sessions.values(),
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
        ttl_seconds=settings.codex_session_ttl_seconds,
        lease_seconds=settings.codex_session_lease_seconds,
        dispatch_queue_capacity=settings.codex_session_queue_capacity,
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
        worker = CodexSessionWorker(
            CodexTransport(app_servers),
            tunnel,
            settings,
        )
        await worker.run()
    finally:
        WORKER_READY.set(0)
        await sandboxes.close()
        await tunnel.close()
