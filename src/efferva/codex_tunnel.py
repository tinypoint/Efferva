from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from redis.asyncio import Redis
from redis.exceptions import ResponseError


class CodexTunnelQueueFullError(RuntimeError):
    pass


class CodexTunnelBackpressureError(RuntimeError):
    pass


class RedisCodexTunnel:
    """Redis-backed, opaque, ordered transport for Codex WebSocket frames."""

    def __init__(
        self,
        url: str,
        *,
        prefix: str = "efferva",
        ttl_seconds: int = 24 * 60 * 60,
        lease_seconds: int = 30,
        dispatch_queue_capacity: int = 10_000,
        frame_queue_capacity: int = 1_000,
        frame_max_bytes: int = 128 * 1024 * 1024,
    ) -> None:
        self._redis = Redis.from_url(url, decode_responses=True)
        self._prefix = prefix.rstrip(":")
        self._ttl_seconds = ttl_seconds
        self._lease_seconds = lease_seconds
        self._dispatch_queue_capacity = dispatch_queue_capacity
        self._frame_queue_capacity = frame_queue_capacity
        self._frame_max_bytes = frame_max_bytes
        self._dispatch_stream = f"{self._prefix}:codex-connections"
        self._dispatch_group = f"{self._prefix}:codex-workers"

    @property
    def heartbeat_interval_seconds(self) -> int:
        return max(1, self._lease_seconds // 3)

    async def open(self) -> None:
        await self._redis.ping()
        try:
            await self._redis.xgroup_create(
                self._dispatch_stream,
                self._dispatch_group,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def ping(self) -> None:
        await self._redis.ping()

    async def close(self) -> None:
        await self._redis.aclose()

    async def open_connection(self, connection_id: str, session_id: str) -> str:
        command = _encode(
            {
                "connectionId": connection_id,
                "sessionId": session_id,
            }
        )
        result = await self._redis.eval(
            """
            if redis.call('xlen', KEYS[2]) >= tonumber(ARGV[3]) then
              return '__CODEX_TUNNEL_QUEUE_FULL__'
            end
            if not redis.call('set', KEYS[1], '1', 'EX', ARGV[1], 'NX') then
              return ''
            end
            redis.call('hset', KEYS[3], 'status', '"queued"')
            redis.call('expire', KEYS[3], ARGV[1])
            redis.call('set', KEYS[4], '1', 'EX', ARGV[2])
            return redis.call('xadd', KEYS[2], '*', 'command', ARGV[4])
            """,
            4,
            self._enqueued_key(connection_id),
            self._dispatch_stream,
            self._state_key(connection_id),
            self._client_key(connection_id),
            self._ttl_seconds,
            self._lease_seconds,
            self._dispatch_queue_capacity,
            command,
        )
        result = str(result or "")
        if result == "__CODEX_TUNNEL_QUEUE_FULL__":
            raise CodexTunnelQueueFullError("the Codex connection queue is full")
        return result

    async def claim_new_connections(
        self,
        worker_id: str,
        *,
        count: int,
        block_ms: int = 1000,
    ) -> list[tuple[str, dict[str, Any]]]:
        if count <= 0:
            return []
        rows = await self._redis.xreadgroup(
            self._dispatch_group,
            worker_id,
            {self._dispatch_stream: ">"},
            count=count,
            block=block_ms,
        )
        return _dispatch_rows(rows)

    async def reclaim_stale_connections(
        self,
        worker_id: str,
        *,
        min_idle_ms: int,
        count: int,
    ) -> list[tuple[str, dict[str, Any]]]:
        if count <= 0:
            return []
        result = await self._redis.xautoclaim(
            self._dispatch_stream,
            self._dispatch_group,
            worker_id,
            min_idle_ms,
            "0-0",
            count=count,
        )
        rows = result[1] if len(result) > 1 else []
        return [
            (str(entry_id), _decode(fields.get("command")))
            for entry_id, fields in rows
        ]

    async def acknowledge_connection(self, dispatch_id: str) -> None:
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xack(
                self._dispatch_stream,
                self._dispatch_group,
                dispatch_id,
            )
            pipeline.xdel(self._dispatch_stream, dispatch_id)
            await pipeline.execute()

    async def touch_connection_claim(
        self,
        worker_id: str,
        dispatch_id: str,
    ) -> None:
        await self._redis.xclaim(
            self._dispatch_stream,
            self._dispatch_group,
            worker_id,
            min_idle_time=0,
            message_ids=[dispatch_id],
            justid=True,
        )

    async def acquire_connection_lease(
        self,
        connection_id: str,
        worker_id: str,
    ) -> bool:
        return bool(
            await self._redis.set(
                self._lease_key(connection_id),
                worker_id,
                ex=self._lease_seconds,
                nx=True,
            )
        )

    async def renew_connection_lease(
        self,
        connection_id: str,
        worker_id: str,
    ) -> bool:
        result = await self._redis.eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
              return redis.call('expire', KEYS[1], ARGV[2])
            end
            return 0
            """,
            1,
            self._lease_key(connection_id),
            worker_id,
            self._lease_seconds,
        )
        return bool(result)

    async def release_connection_lease(
        self,
        connection_id: str,
        worker_id: str,
    ) -> None:
        await self._redis.eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
              return redis.call('del', KEYS[1])
            end
            return 0
            """,
            1,
            self._lease_key(connection_id),
            worker_id,
        )

    async def touch_client(self, connection_id: str) -> bool:
        return bool(
            await self._redis.expire(
                self._client_key(connection_id),
                self._lease_seconds,
            )
        )

    async def client_is_connected(self, connection_id: str) -> bool:
        return bool(await self._redis.exists(self._client_key(connection_id)))

    async def disconnect_client(self, connection_id: str) -> None:
        try:
            await self.send_frame(connection_id, "client", kind="close")
        finally:
            await self._redis.delete(self._client_key(connection_id))

    async def send_frame(
        self,
        connection_id: str,
        direction: Literal["client", "server"],
        *,
        payload: str = "",
        kind: Literal["text", "close"] = "text",
    ) -> str:
        if len(payload.encode()) > self._frame_max_bytes:
            raise ValueError("Codex frame exceeds the transport size limit")
        key = self._frame_stream(connection_id, direction)
        result = await self._redis.eval(
            """
            if redis.call('xlen', KEYS[1]) >= tonumber(ARGV[1]) then
              return '__CODEX_TUNNEL_BACKPRESSURE__'
            end
            local id = redis.call(
              'xadd', KEYS[1], '*', 'kind', ARGV[2], 'payload', ARGV[3]
            )
            redis.call('expire', KEYS[1], ARGV[4])
            return id
            """,
            1,
            key,
            self._frame_queue_capacity,
            kind,
            payload,
            self._ttl_seconds,
        )
        result = str(result or "")
        if result == "__CODEX_TUNNEL_BACKPRESSURE__":
            raise CodexTunnelBackpressureError(
                f"the Codex {direction} frame queue is full"
            )
        return result

    async def read_frames(
        self,
        connection_id: str,
        direction: Literal["client", "server"],
        *,
        after: str,
        block_ms: int = 15_000,
    ) -> list[tuple[str, str, str]]:
        rows = await self._redis.xread(
            {self._frame_stream(connection_id, direction): after},
            block=block_ms,
        )
        return [
            (
                str(entry_id),
                str(fields.get("kind") or "text"),
                str(fields.get("payload") or ""),
            )
            for _, entries in rows
            for entry_id, fields in entries
        ]

    async def acknowledge_frame(
        self,
        connection_id: str,
        direction: Literal["client", "server"],
        frame_id: str,
    ) -> None:
        await self._redis.xdel(
            self._frame_stream(connection_id, direction),
            frame_id,
        )

    async def set_state(
        self,
        connection_id: str,
        values: Mapping[str, Any],
    ) -> None:
        encoded = {name: _encode(value) for name, value in values.items()}
        if not encoded:
            return
        key = self._state_key(connection_id)
        await self._redis.hset(key, mapping=encoded)
        await self._redis.expire(key, self._ttl_seconds)

    async def get_state(self, connection_id: str) -> dict[str, Any]:
        values = await self._redis.hgetall(self._state_key(connection_id))
        return {name: _decode(value) for name, value in values.items()}

    def _frame_stream(
        self,
        connection_id: str,
        direction: Literal["client", "server"],
    ) -> str:
        return f"{self._prefix}:codex:{connection_id}:{direction}"

    def _state_key(self, connection_id: str) -> str:
        return f"{self._prefix}:codex:{connection_id}:state"

    def _client_key(self, connection_id: str) -> str:
        return f"{self._prefix}:codex:{connection_id}:client-presence"

    def _lease_key(self, connection_id: str) -> str:
        return f"{self._prefix}:codex:{connection_id}:lease"

    def _enqueued_key(self, connection_id: str) -> str:
        return f"{self._prefix}:codex:{connection_id}:enqueued"


def _dispatch_rows(rows: Any) -> list[tuple[str, dict[str, Any]]]:
    return [
        (str(entry_id), _decode(fields.get("command")))
        for _, entries in rows
        for entry_id, fields in entries
    ]


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)
