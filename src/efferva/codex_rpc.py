from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from efferva.codex_appserver import CodexAppServerManager


class CodexRpcError(RuntimeError):
    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.method = method
        self.error = error
        super().__init__(f"{method}: {error.get('message', error)}")


ServerRequestHandler = Callable[
    [Mapping[str, Any], str, Mapping[str, Any]],
    Mapping[str, Any] | Awaitable[Mapping[str, Any]],
]


class CodexConnection:
    """One initialized JSON-RPC connection to a Session's Codex app-server."""

    def __init__(
        self,
        client: CodexRpcClient,
        websocket: ClientConnection,
        session: Mapping[str, Any],
    ) -> None:
        self._client = client
        self._websocket = websocket
        self._session = session

    async def request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._client._request(
            self._websocket,
            self._session,
            method,
            params,
        )

    async def start_request(
        self,
        method: str,
        params: dict[str, Any],
    ) -> int:
        request_id = await self._client._request_id()
        await self._websocket.send(
            json.dumps(
                {"method": method, "id": request_id, "params": params},
                separators=(",", ":"),
            )
        )
        return request_id

    async def receive(self) -> dict[str, Any]:
        return dict(json.loads(await self._websocket.recv()))

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._websocket.send(
            json.dumps(
                {"method": method, "params": params},
                separators=(",", ":"),
            )
        )

    async def handle_server_request(self, message: Mapping[str, Any]) -> None:
        await self._client._handle_server_request(
            self._websocket,
            self._session,
            message,
        )


class CodexRpcClient:
    """Owns authenticated WebSocket connections and Codex JSON-RPC framing."""

    def __init__(
        self,
        app_servers: CodexAppServerManager,
        *,
        server_request_handler: ServerRequestHandler | None = None,
    ) -> None:
        self._app_servers = app_servers
        self._server_request_handler = server_request_handler
        self._next_id = 1
        self._id_lock = asyncio.Lock()

    def set_server_request_handler(
        self,
        handler: ServerRequestHandler | None,
    ) -> None:
        self._server_request_handler = handler

    async def request(
        self,
        session: Mapping[str, Any],
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.connection(session) as connection:
            return await connection.request(method, params or {})

    @asynccontextmanager
    async def connection(
        self,
        session: Mapping[str, Any],
    ) -> AsyncIterator[CodexConnection]:
        url, headers = await self._app_servers.connection_target(session)
        last_error: Exception | None = None
        for attempt in range(8):
            try:
                websocket = await connect(
                    url,
                    additional_headers=headers,
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
            connection = CodexConnection(self, websocket, session)
            await self._initialize(connection)
            yield connection
        finally:
            await websocket.close()

    async def _initialize(self, connection: CodexConnection) -> None:
        await connection.request(
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
        await connection.notify("initialized", {})

    async def _request(
        self,
        websocket: ClientConnection,
        session: Mapping[str, Any],
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
            message = dict(json.loads(await websocket.recv()))
            if message.get("id") == request_id:
                if "error" in message:
                    raise CodexRpcError(method, dict(message["error"]))
                return dict(message.get("result") or {})
            if "method" in message and "id" in message:
                await self._handle_server_request(websocket, session, message)

    async def _handle_server_request(
        self,
        websocket: ClientConnection,
        session: Mapping[str, Any],
        message: Mapping[str, Any],
    ) -> None:
        method = str(message["method"])
        raw_params = message.get("params")
        params = raw_params if isinstance(raw_params, Mapping) else {}
        try:
            if self._server_request_handler is not None:
                response = self._server_request_handler(session, method, params)
                if isinstance(response, Awaitable):
                    response = await response
                payload: dict[str, Any] = {
                    "id": message["id"],
                    "result": dict(response),
                }
            else:
                default = default_server_response(method, params)
                if default is None:
                    raise NotImplementedError(f"unsupported server request: {method}")
                payload = {"id": message["id"], "result": default}
        except Exception as error:
            payload = {
                "id": message["id"],
                "error": {"code": -32000, "message": str(error)},
            }
        await websocket.send(json.dumps(payload, separators=(",", ":")))

    async def _request_id(self) -> int:
        async with self._id_lock:
            value = self._next_id
            self._next_id += 1
            return value


def default_server_response(
    method: str,
    params: Mapping[str, Any],
) -> dict[str, Any] | None:
    if method == "item/tool/call":
        tool = str(params.get("tool") or "dynamic tool")
        return {
            "contentItems": [
                {
                    "type": "inputText",
                    "text": f"No Efferva handler is registered for {tool}.",
                }
            ],
            "success": False,
        }
    if method == "item/tool/requestUserInput":
        questions = params.get("questions") or []
        return {
            "answers": {
                str(question["id"]): {"answers": []}
                for question in questions
                if isinstance(question, Mapping) and question.get("id")
            }
        }
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        return {"decision": "decline"}
    if method == "mcpServer/elicitation/request":
        return {"action": "decline", "content": None, "_meta": None}
    if method == "item/permissions/requestApproval":
        return {"permissions": {}, "scope": "turn"}
    return None
