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
    """Redis transport between browser clients and Session-level Codex channels."""

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
        self._dispatch_stream = f"{self._prefix}:codex-channels"
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

    async def attach_client(
        self,
        session_id: str,
        client_id: str,
        candidate_channel_id: str,
    ) -> str:
        command = _encode(
            {
                "channelId": candidate_channel_id,
                "sessionId": session_id,
            }
        )
        result = await self._redis.eval(
            """
            local channel = redis.call('get', KEYS[2])
            if channel then
              local state_key = ARGV[1] .. channel .. ':state'
              local status = redis.call('hget', state_key, 'status')
              if status == ARGV[9] or status == ARGV[10] or status == ARGV[11] then
                redis.call('del', KEYS[2])
                channel = false
              end
            end

            if not channel then
              if redis.call('xlen', KEYS[1]) >= tonumber(ARGV[4]) then
                return '__CODEX_TUNNEL_QUEUE_FULL__'
              end
              channel = ARGV[7]
              redis.call('set', KEYS[2], channel, 'EX', ARGV[2])
              local base = ARGV[1] .. channel
              redis.call('set', base .. ':enqueued', '1', 'EX', ARGV[2])
              redis.call('hset', base .. ':state',
                'status', ARGV[8], 'sessionId', ARGV[12])
              redis.call('expire', base .. ':state', ARGV[2])
              redis.call('xadd', KEYS[1], '*', 'command', ARGV[13])
            else
              redis.call('expire', KEYS[2], ARGV[2])
            end

            local base = ARGV[1] .. channel
            redis.call('sadd', base .. ':clients', ARGV[6])
            redis.call('expire', base .. ':clients', ARGV[2])
            redis.call('set',
              base .. ':client:' .. ARGV[6] .. ':presence',
              '1', 'EX', ARGV[3])
            return channel
            """,
            2,
            self._dispatch_stream,
            self._current_channel_key(session_id),
            f"{self._prefix}:codex:",
            self._ttl_seconds,
            self._lease_seconds,
            self._dispatch_queue_capacity,
            session_id,
            client_id,
            candidate_channel_id,
            _encode("queued"),
            _encode("closing"),
            _encode("closed"),
            _encode("failed"),
            _encode(session_id),
            command,
        )
        result = str(result or "")
        if result == "__CODEX_TUNNEL_QUEUE_FULL__":
            raise CodexTunnelQueueFullError("the Codex Session queue is full")
        if not result:
            raise RuntimeError("Redis did not attach the Codex client")
        return result

    async def detach_client(self, channel_id: str, client_id: str) -> None:
        await self._redis.eval(
            """
            redis.call('xadd', KEYS[1], '*',
              'clientId', ARGV[1], 'kind', 'close', 'payload', '')
            redis.call('expire', KEYS[1], ARGV[2])
            redis.call('srem', KEYS[2], ARGV[1])
            redis.call('del', KEYS[3])
            """,
            3,
            self._incoming_stream(channel_id),
            self._clients_key(channel_id),
            self._client_presence_key(channel_id, client_id),
            client_id,
            self._ttl_seconds,
        )

    async def touch_client(self, channel_id: str, client_id: str) -> bool:
        result = await self._redis.eval(
            """
            if redis.call('exists', KEYS[1]) == 0 then
              return 0
            end
            redis.call('expire', KEYS[1], ARGV[1])
            redis.call('expire', KEYS[2], ARGV[2])
            redis.call('expire', KEYS[3], ARGV[2])
            redis.call('expire', KEYS[4], ARGV[2])
            return 1
            """,
            4,
            self._client_presence_key(channel_id, client_id),
            self._clients_key(channel_id),
            self._state_key(channel_id),
            self._incoming_stream(channel_id),
            self._lease_seconds,
            self._ttl_seconds,
        )
        return bool(result)

    async def active_clients(self, channel_id: str) -> list[str]:
        clients = sorted(await self._redis.smembers(self._clients_key(channel_id)))
        if not clients:
            return []
        presence = await self._redis.mget(
            [self._client_presence_key(channel_id, client_id) for client_id in clients]
        )
        active = [
            client_id
            for client_id, present in zip(clients, presence, strict=True)
            if present is not None
        ]
        stale = set(clients) - set(active)
        if stale:
            await self._redis.srem(self._clients_key(channel_id), *stale)
        return active

    async def send_client_frame(
        self,
        channel_id: str,
        client_id: str,
        *,
        payload: str,
    ) -> str:
        self._validate_frame(payload)
        result = await self._redis.eval(
            """
            if redis.call('exists', KEYS[2]) == 0 then
              return ''
            end
            if redis.call('xlen', KEYS[1]) >= tonumber(ARGV[1]) then
              return '__CODEX_TUNNEL_BACKPRESSURE__'
            end
            local id = redis.call('xadd', KEYS[1], '*',
              'clientId', ARGV[2], 'kind', 'text', 'payload', ARGV[3])
            redis.call('expire', KEYS[1], ARGV[4])
            return id
            """,
            2,
            self._incoming_stream(channel_id),
            self._client_presence_key(channel_id, client_id),
            self._frame_queue_capacity,
            client_id,
            payload,
            self._ttl_seconds,
        )
        return self._frame_result(result, "incoming")

    async def read_client_frames(
        self,
        channel_id: str,
        *,
        after: str,
        block_ms: int = 15_000,
    ) -> list[tuple[str, str, str, str]]:
        rows = await self._redis.xread(
            {self._incoming_stream(channel_id): after},
            block=block_ms,
        )
        return [
            (
                str(entry_id),
                str(fields.get("clientId") or ""),
                str(fields.get("kind") or "text"),
                str(fields.get("payload") or ""),
            )
            for _, entries in rows
            for entry_id, fields in entries
        ]

    async def acknowledge_client_frame(
        self,
        channel_id: str,
        frame_id: str,
    ) -> None:
        await self._redis.xdel(self._incoming_stream(channel_id), frame_id)

    async def send_server_frame(
        self,
        channel_id: str,
        client_id: str,
        *,
        payload: str = "",
        kind: Literal["text", "close"] = "text",
    ) -> str:
        self._validate_frame(payload)
        result = await self._redis.eval(
            """
            if redis.call('exists', KEYS[2]) == 0 then
              return ''
            end
            if redis.call('xlen', KEYS[1]) >= tonumber(ARGV[1]) then
              return '__CODEX_TUNNEL_BACKPRESSURE__'
            end
            local id = redis.call('xadd', KEYS[1], '*',
              'kind', ARGV[2], 'payload', ARGV[3])
            redis.call('expire', KEYS[1], ARGV[4])
            return id
            """,
            2,
            self._outgoing_stream(channel_id, client_id),
            self._client_presence_key(channel_id, client_id),
            self._frame_queue_capacity,
            kind,
            payload,
            self._ttl_seconds,
        )
        return self._frame_result(result, f"outgoing client {client_id}")

    async def read_server_frames(
        self,
        channel_id: str,
        client_id: str,
        *,
        after: str,
        block_ms: int = 15_000,
    ) -> list[tuple[str, str, str]]:
        rows = await self._redis.xread(
            {self._outgoing_stream(channel_id, client_id): after},
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

    async def acknowledge_server_frame(
        self,
        channel_id: str,
        client_id: str,
        frame_id: str,
    ) -> None:
        await self._redis.xdel(
            self._outgoing_stream(channel_id, client_id),
            frame_id,
        )

    async def claim_new_channels(
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

    async def reclaim_stale_channels(
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

    async def touch_channel_claim(self, worker_id: str, dispatch_id: str) -> None:
        await self._redis.xclaim(
            self._dispatch_stream,
            self._dispatch_group,
            worker_id,
            min_idle_time=0,
            message_ids=[dispatch_id],
            justid=True,
        )

    async def acquire_channel_lease(
        self,
        channel_id: str,
        worker_id: str,
    ) -> bool:
        return bool(
            await self._redis.set(
                self._lease_key(channel_id),
                worker_id,
                ex=self._lease_seconds,
                nx=True,
            )
        )

    async def renew_channel_lease(
        self,
        session_id: str,
        channel_id: str,
        worker_id: str,
    ) -> bool:
        result = await self._redis.eval(
            """
            if redis.call('get', KEYS[1]) ~= ARGV[1] then
              return 0
            end
            redis.call('expire', KEYS[1], ARGV[2])
            if redis.call('get', KEYS[2]) == ARGV[3] then
              redis.call('expire', KEYS[2], ARGV[4])
            end
            redis.call('expire', KEYS[3], ARGV[4])
            redis.call('expire', KEYS[4], ARGV[4])
            redis.call('expire', KEYS[5], ARGV[4])
            return 1
            """,
            5,
            self._lease_key(channel_id),
            self._current_channel_key(session_id),
            self._enqueued_key(channel_id),
            self._state_key(channel_id),
            self._clients_key(channel_id),
            worker_id,
            self._lease_seconds,
            channel_id,
            self._ttl_seconds,
        )
        return bool(result)

    async def release_channel(
        self,
        session_id: str,
        channel_id: str,
        worker_id: str,
        dispatch_id: str,
    ) -> None:
        await self._redis.eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
              redis.call('del', KEYS[1])
            end
            if redis.call('get', KEYS[2]) == ARGV[2] then
              redis.call('del', KEYS[2])
            end
            redis.call('del', KEYS[3])
            redis.call('xack', KEYS[4], ARGV[3], ARGV[4])
            redis.call('xdel', KEYS[4], ARGV[4])
            """,
            4,
            self._current_channel_key(session_id),
            self._lease_key(channel_id),
            self._enqueued_key(channel_id),
            self._dispatch_stream,
            channel_id,
            worker_id,
            self._dispatch_group,
            dispatch_id,
        )

    async def set_state(
        self,
        channel_id: str,
        values: Mapping[str, Any],
    ) -> None:
        encoded = {name: _encode(value) for name, value in values.items()}
        if not encoded:
            return
        key = self._state_key(channel_id)
        await self._redis.hset(key, mapping=encoded)
        await self._redis.expire(key, self._ttl_seconds)

    async def get_state(self, channel_id: str) -> dict[str, Any]:
        values = await self._redis.hgetall(self._state_key(channel_id))
        return {name: _decode(value) for name, value in values.items()}

    def _frame_result(self, result: Any, queue: str) -> str:
        normalized = str(result or "")
        if normalized == "__CODEX_TUNNEL_BACKPRESSURE__":
            raise CodexTunnelBackpressureError(
                f"the Codex {queue} frame queue is full"
            )
        return normalized

    def _validate_frame(self, payload: str) -> None:
        if len(payload.encode()) > self._frame_max_bytes:
            raise ValueError("Codex frame exceeds the transport size limit")

    def _current_channel_key(self, session_id: str) -> str:
        return f"{self._prefix}:codex-session:{session_id}:channel"

    def _base(self, channel_id: str) -> str:
        return f"{self._prefix}:codex:{channel_id}"

    def _clients_key(self, channel_id: str) -> str:
        return f"{self._base(channel_id)}:clients"

    def _client_presence_key(self, channel_id: str, client_id: str) -> str:
        return f"{self._base(channel_id)}:client:{client_id}:presence"

    def _incoming_stream(self, channel_id: str) -> str:
        return f"{self._base(channel_id)}:incoming"

    def _outgoing_stream(self, channel_id: str, client_id: str) -> str:
        return f"{self._base(channel_id)}:client:{client_id}:outgoing"

    def _state_key(self, channel_id: str) -> str:
        return f"{self._base(channel_id)}:state"

    def _lease_key(self, channel_id: str) -> str:
        return f"{self._base(channel_id)}:lease"

    def _enqueued_key(self, channel_id: str) -> str:
        return f"{self._base(channel_id)}:enqueued"


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
