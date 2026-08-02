from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from efferva.codex_tunnel import (
    CodexTunnelBackpressureError,
    CodexTunnelQueueFullError,
    RedisCodexTunnel,
)
from efferva.identity import (
    ForbiddenError,
    IdentityResolver,
    Principal,
    UnauthenticatedError,
)
from efferva.models import PrincipalView, Session, SessionCreate
from efferva.repository import (
    AccessMode,
    NotFoundError,
    SessionRepository,
)


def principal_dependency(
    identity: IdentityResolver,
) -> Callable[[Request], Any]:
    async def resolve(request: Request) -> Principal:
        principal = await identity(request)
        if not isinstance(principal, Principal):
            raise TypeError("IdentityResolver must return efferva.Principal")
        return principal

    return resolve


def create_api_router(
    *,
    identity: IdentityResolver,
    repository: Callable[[], SessionRepository],
    codex_tunnel: Callable[[], RedisCodexTunnel],
) -> APIRouter:
    router = APIRouter()
    resolve_principal = principal_dependency(identity)

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        await repository().ping()
        await codex_tunnel().ping()
        return {"status": "ok"}

    @router.websocket("/api/sessions/{session_id}/codex")
    async def codex_websocket(
        websocket: WebSocket,
        session_id: UUID,
    ) -> None:
        try:
            principal = await identity(websocket)
            if not isinstance(principal, Principal):
                raise TypeError("IdentityResolver must return efferva.Principal")
            await repository().get_session(
                principal,
                session_id,
                mode=AccessMode.WRITE,
                touch=True,
            )
        except UnauthenticatedError:
            await websocket.close(code=4401)
            return
        except ForbiddenError:
            await websocket.close(code=4403)
            return
        except NotFoundError:
            await websocket.close(code=4404)
            return

        connection_id = str(uuid4())
        tunnel = codex_tunnel()
        try:
            await tunnel.open_connection(connection_id, str(session_id))
        except CodexTunnelQueueFullError:
            await websocket.close(code=1013, reason="Codex workers are busy")
            return
        await websocket.accept()

        async def client_heartbeat() -> None:
            while True:
                await asyncio.sleep(tunnel.heartbeat_interval_seconds)
                if not await tunnel.touch_client(connection_id):
                    return

        async def client_to_worker() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                payload = message.get("text")
                if payload is None:
                    await websocket.close(
                        code=1003,
                        reason="Codex JSON-RPC requires text frames",
                    )
                    return
                await tunnel.send_frame(
                    connection_id,
                    "client",
                    payload=payload,
                )

        async def worker_to_client() -> None:
            cursor = "0-0"
            while True:
                frames = await tunnel.read_frames(
                    connection_id,
                    "server",
                    after=cursor,
                )
                if not frames:
                    state = await tunnel.get_state(connection_id)
                    if state.get("status") in {"closed", "failed"}:
                        await websocket.close(
                            code=1011 if state.get("status") == "failed" else 1000,
                            reason=(
                                "Codex connection failed"
                                if state.get("status") == "failed"
                                else "Codex connection closed"
                            ),
                        )
                        return
                    continue
                for frame_id, kind, payload in frames:
                    cursor = frame_id
                    if kind == "close":
                        await tunnel.acknowledge_frame(
                            connection_id,
                            "server",
                            frame_id,
                        )
                        state = await tunnel.get_state(connection_id)
                        await websocket.close(
                            code=1011 if state.get("status") == "failed" else 1000,
                            reason=(
                                "Codex connection failed"
                                if state.get("status") == "failed"
                                else "Codex connection closed"
                            ),
                        )
                        return
                    await websocket.send_text(payload)
                    await tunnel.acknowledge_frame(
                        connection_id,
                        "server",
                        frame_id,
                    )

        tasks = {
            asyncio.create_task(client_heartbeat()),
            asyncio.create_task(client_to_worker()),
            asyncio.create_task(worker_to_client()),
        }
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except (CodexTunnelBackpressureError, ValueError, WebSocketDisconnect):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(CodexTunnelBackpressureError):
                await tunnel.disconnect_client(connection_id)
            if websocket.client_state == WebSocketState.CONNECTED:
                with suppress(RuntimeError):
                    await websocket.close()

    @router.get("/api/meta", include_in_schema=False)
    async def metadata(
        request: Request,
        _: Principal = Depends(resolve_principal),
    ) -> dict[str, str]:
        return {"title": request.app.title}

    @router.get("/api/me", response_model=PrincipalView)
    async def me(
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        return {
            "tenant_id": principal.tenant_id,
            "issuer": principal.issuer,
            "subject": principal.subject,
            "capabilities": sorted(principal.capabilities, key=lambda item: item.value),
        }

    @router.post("/api/sessions", response_model=Session, status_code=201)
    async def create_session(
        payload: SessionCreate,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        return await repository().create_session(principal, payload.name)

    @router.get("/api/sessions", response_model=list[Session])
    async def list_sessions(
        principal: Principal = Depends(resolve_principal),
        scope: Literal["mine", "tenant"] = Query(default="mine"),
    ) -> list[dict[str, Any]]:
        return await repository().list_sessions(principal, scope)

    return router
