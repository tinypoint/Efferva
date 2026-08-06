from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState
from websockets.exceptions import ConnectionClosed

from efferva.codex_appserver import CodexAppServerManager
from efferva.db import Database
from efferva.identity import (
    ForbiddenError,
    IdentityResolver,
    Principal,
    UnauthenticatedError,
)
from efferva.sandbox.service import SessionSandboxService
from efferva.session_repository import AccessMode, NotFoundError, SessionRepository


@dataclass(frozen=True, slots=True)
class Codex:
    api_key: str | None = None
    base_url: str | None = None

    name = "codex"
    protocol = "codex-app-server-websocket"

    def create_router(
        self,
        *,
        identity: IdentityResolver,
        repository: SessionRepository,
        sandboxes: SessionSandboxService,
        database: Database,
    ) -> APIRouter:
        manager = CodexAppServerManager(
            api_key=self.api_key,
            openai_base_url=self.base_url,
            sandboxes=sandboxes,
            database=database,
        )
        router = APIRouter()

        @router.websocket("/api/sessions/{session_id}/codex")
        async def codex_websocket(websocket: WebSocket, session_id: UUID) -> None:
            try:
                principal = await identity(websocket)
                if not isinstance(principal, Principal):
                    raise TypeError("IdentityResolver must return efferva.Principal")
                session = await repository.get_session(
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
                upstream_context = manager.connect(session)
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

        return router
