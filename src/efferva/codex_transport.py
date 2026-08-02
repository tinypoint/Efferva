from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from efferva.codex_appserver import CodexAppServerManager


class CodexTransport:
    """Opens an authenticated WebSocket to a Session's Codex app-server."""

    def __init__(self, app_servers: CodexAppServerManager) -> None:
        self._app_servers = app_servers

    @asynccontextmanager
    async def connect(
        self,
        session: Mapping[str, Any],
    ) -> AsyncIterator[ClientConnection]:
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
                    max_size=128 * 1024 * 1024,
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
            yield websocket
        finally:
            await websocket.close()
