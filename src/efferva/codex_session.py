from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection

from efferva.codex_tunnel import RedisCodexTunnel


@dataclass(slots=True)
class _PendingRequest:
    client_id: str
    client_request_id: Any
    method: str
    params: dict[str, Any]


class CodexSession:
    """Multiplexes browser clients over one Codex app-server connection."""

    def __init__(
        self,
        channel_id: str,
        upstream: ClientConnection,
        tunnel: RedisCodexTunnel,
    ) -> None:
        self._channel_id = channel_id
        self._upstream = upstream
        self._tunnel = tunnel
        self._initialize_result: dict[str, Any] = {}
        self._next_request_id = 1
        self._pending_requests: dict[int, _PendingRequest] = {}
        self._pending_turn_starts: set[int] = set()
        self._pending_server_requests: dict[tuple[str, str], Any] = {}
        self._initialized_clients: set[str] = set()
        self._thread_owners: dict[str, str] = {}
        self._active_threads: set[str] = set()
        self._active_turns: set[str] = set()
        self._last_active_client: str | None = None

    async def initialize(self) -> None:
        request_id = 0
        await self._send_upstream(
            {
                "id": request_id,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "efferva-worker",
                        "title": "Efferva Worker",
                        "version": "0.1.0",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "requestAttestation": False,
                    },
                },
            }
        )
        while True:
            message = self._parse(await self._upstream.recv())
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(
                        f"Codex initialize failed: {message['error']}"
                    )
                result = message.get("result")
                self._initialize_result = (
                    dict(result) if isinstance(result, dict) else {}
                )
                break
            if "method" in message and "id" in message:
                await self._send_upstream(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "No browser client is initialized yet",
                        },
                    }
                )
        await self._send_upstream({"method": "initialized", "params": {}})

    async def run(self) -> None:
        tasks = {
            asyncio.create_task(self._clients_to_server()),
            asyncio.create_task(self._server_to_clients()),
            asyncio.create_task(self._watch_clients()),
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

    async def _clients_to_server(self) -> None:
        cursor = "0-0"
        while True:
            frames = await self._tunnel.read_client_frames(
                self._channel_id,
                after=cursor,
            )
            for frame_id, client_id, kind, payload in frames:
                cursor = frame_id
                try:
                    if kind == "close":
                        await self._client_closed(client_id)
                    else:
                        await self._handle_client_message(client_id, payload)
                finally:
                    await self._tunnel.acknowledge_client_frame(
                        self._channel_id,
                        frame_id,
                    )

    async def _server_to_clients(self) -> None:
        async for payload in self._upstream:
            message = self._parse(payload)
            if "method" in message:
                if "id" in message:
                    await self._route_server_request(message)
                else:
                    self._observe_notification(message)
                    await self._broadcast(message)
                continue
            await self._route_server_response(message)

    async def _watch_clients(self) -> None:
        empty_checks = 0
        while True:
            await asyncio.sleep(self._tunnel.heartbeat_interval_seconds)
            active = set(await self._tunnel.active_clients(self._channel_id))
            self._initialized_clients.intersection_update(active)
            if active or self._has_active_work():
                empty_checks = 0
                continue
            empty_checks += 1
            if empty_checks >= 2:
                return

    async def _handle_client_message(
        self,
        client_id: str,
        payload: str,
    ) -> None:
        message = self._parse(payload)
        method = message.get("method")
        if method == "initialize" and "id" in message:
            self._initialized_clients.add(client_id)
            await self._send_client(
                client_id,
                {"id": message["id"], "result": self._initialize_result},
            )
            return
        if method == "initialized":
            return
        if method is not None:
            self._last_active_client = client_id
            if "id" not in message:
                await self._send_upstream(message)
                return
            await self._forward_client_request(client_id, message)
            return
        if "id" in message:
            await self._forward_server_response(client_id, message)

    async def _forward_client_request(
        self,
        client_id: str,
        message: dict[str, Any],
    ) -> None:
        upstream_id = self._next_request_id
        self._next_request_id += 1
        method = str(message["method"])
        params = message.get("params")
        normalized_params = dict(params) if isinstance(params, dict) else {}
        self._pending_requests[upstream_id] = _PendingRequest(
            client_id=client_id,
            client_request_id=message["id"],
            method=method,
            params=normalized_params,
        )
        thread_id = _optional_string(normalized_params.get("threadId"))
        if thread_id and method in {
            "turn/start",
            "turn/steer",
            "turn/interrupt",
        }:
            self._thread_owners[thread_id] = client_id
        if method == "turn/start":
            self._pending_turn_starts.add(upstream_id)
            if thread_id:
                self._active_threads.add(thread_id)
        forwarded = dict(message)
        forwarded["id"] = upstream_id
        await self._send_upstream(forwarded)

    async def _route_server_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        pending = self._pending_requests.pop(request_id, None)
        if pending is None:
            return
        self._pending_turn_starts.discard(request_id)
        self._observe_response(pending, message)
        routed = dict(message)
        routed["id"] = pending.client_request_id
        await self._send_client(pending.client_id, routed)

    async def _route_server_request(self, message: dict[str, Any]) -> None:
        clients = sorted(self._initialized_clients)
        params = message.get("params")
        normalized_params = dict(params) if isinstance(params, dict) else {}
        thread_id = _optional_string(normalized_params.get("threadId"))
        client_id = self._thread_owners.get(thread_id or "")
        if client_id not in self._initialized_clients:
            client_id = self._last_active_client
        if client_id not in self._initialized_clients:
            client_id = clients[0] if clients else None
        if client_id is None:
            await self._send_upstream(
                {
                    "id": message["id"],
                    "error": {
                        "code": -32000,
                        "message": "No browser client can handle this request",
                    },
                }
            )
            return
        self._pending_server_requests[
            (client_id, _rpc_id(message["id"]))
        ] = message["id"]
        await self._send_client(client_id, message)

    async def _forward_server_response(
        self,
        client_id: str,
        message: dict[str, Any],
    ) -> None:
        request_id = self._pending_server_requests.pop(
            (client_id, _rpc_id(message["id"])),
            None,
        )
        if request_id is None:
            return
        forwarded = dict(message)
        forwarded["id"] = request_id
        await self._send_upstream(forwarded)

    async def _client_closed(self, client_id: str) -> None:
        self._initialized_clients.discard(client_id)
        if self._last_active_client == client_id:
            self._last_active_client = None
        self._thread_owners = {
            thread_id: owner
            for thread_id, owner in self._thread_owners.items()
            if owner != client_id
        }
        abandoned = [
            (key, request_id)
            for key, request_id in self._pending_server_requests.items()
            if key[0] == client_id
        ]
        for key, request_id in abandoned:
            self._pending_server_requests.pop(key, None)
            await self._send_upstream(
                {
                    "id": request_id,
                    "error": {
                        "code": -32000,
                        "message": "The browser client disconnected",
                    },
                }
            )

    def _observe_response(
        self,
        pending: _PendingRequest,
        message: dict[str, Any],
    ) -> None:
        if "error" in message:
            if pending.method == "turn/start":
                thread_id = _optional_string(pending.params.get("threadId"))
                if thread_id:
                    self._active_threads.discard(thread_id)
            return
        result = message.get("result")
        normalized_result = dict(result) if isinstance(result, dict) else {}
        thread = normalized_result.get("thread")
        normalized_thread = dict(thread) if isinstance(thread, dict) else {}
        thread_id = _optional_string(
            normalized_thread.get("id") or pending.params.get("threadId")
        )
        if thread_id and pending.method in {"thread/start", "turn/start"}:
            self._thread_owners[thread_id] = pending.client_id
        if pending.method == "turn/start":
            turn = normalized_result.get("turn")
            normalized_turn = dict(turn) if isinstance(turn, dict) else {}
            turn_id = _optional_string(normalized_turn.get("id"))
            if turn_id:
                self._active_turns.add(turn_id)
        if pending.method == "thread/resume" and thread_id:
            status = normalized_thread.get("status")
            normalized_status = dict(status) if isinstance(status, dict) else {}
            if normalized_status.get("type") == "active":
                self._active_threads.add(thread_id)

    def _observe_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        normalized_params = dict(params) if isinstance(params, dict) else {}
        thread_id = _optional_string(normalized_params.get("threadId"))
        turn = normalized_params.get("turn")
        normalized_turn = dict(turn) if isinstance(turn, dict) else {}
        turn_id = _optional_string(normalized_turn.get("id"))
        if method == "turn/started":
            if thread_id:
                self._active_threads.add(thread_id)
            if turn_id:
                self._active_turns.add(turn_id)
        elif method == "turn/completed":
            if thread_id:
                self._active_threads.discard(thread_id)
            if turn_id:
                self._active_turns.discard(turn_id)

    def _has_active_work(self) -> bool:
        return bool(
            self._pending_turn_starts
            or self._pending_server_requests
            or self._active_threads
            or self._active_turns
        )

    async def _broadcast(self, message: dict[str, Any]) -> None:
        payload = _serialize(message)
        await asyncio.gather(
            *(
                self._tunnel.send_server_frame(
                    self._channel_id,
                    client_id,
                    payload=payload,
                )
                for client_id in sorted(self._initialized_clients)
            )
        )

    async def _send_client(
        self,
        client_id: str,
        message: dict[str, Any],
    ) -> None:
        await self._tunnel.send_server_frame(
            self._channel_id,
            client_id,
            payload=_serialize(message),
        )

    async def _send_upstream(self, message: dict[str, Any]) -> None:
        await self._upstream.send(_serialize(message))

    @staticmethod
    def _parse(payload: Any) -> dict[str, Any]:
        if not isinstance(payload, str):
            raise TypeError("Codex JSON-RPC requires text frames")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise TypeError("Codex JSON-RPC batch messages are not supported")
        return value


def _serialize(message: dict[str, Any]) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"))


def _rpc_id(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _optional_string(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
