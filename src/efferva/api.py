from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.exceptions import ConnectionClosed

from efferva.codex_appserver import CodexAppServerManager
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
    codex: Callable[[], CodexAppServerManager],
) -> APIRouter:
    router = APIRouter()
    resolve_principal = principal_dependency(identity)

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        await repository().ping()
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
            session = await repository().get_session(
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

        try:
            upstream_context = codex().connect(session)
            upstream = await upstream_context.__aenter__()
        except Exception:
            await websocket.close(code=1011, reason="Codex app-server unavailable")
            return
        await websocket.accept()

        async def client_to_codex() -> None:
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
                await upstream.send(payload)

        async def codex_to_client() -> None:
            async for payload in upstream:
                if not isinstance(payload, str):
                    await websocket.close(
                        code=1003,
                        reason="Codex JSON-RPC requires text frames",
                    )
                    return
                await websocket.send_text(payload)

        tasks = {
            asyncio.create_task(client_to_codex()),
            asyncio.create_task(codex_to_client()),
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
        except (ConnectionClosed, ValueError, WebSocketDisconnect):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream_context.__aexit__(None, None, None)
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
