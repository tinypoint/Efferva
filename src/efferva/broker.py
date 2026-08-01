from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError


class RunQueueFullError(RuntimeError):
    pass


class RedisRunBroker:
    def __init__(
        self,
        url: str,
        *,
        prefix: str = "efferva",
        event_ttl_seconds: int = 24 * 60 * 60,
        event_stream_maxlen: int = 10_000,
        command_stream_maxlen: int = 1_000,
        dispatch_queue_capacity: int = 10_000,
        event_max_bytes: int = 1024 * 1024,
        command_max_bytes: int = 1024 * 1024,
    ) -> None:
        self._redis = Redis.from_url(url, decode_responses=True)
        self._prefix = prefix.rstrip(":")
        self._event_ttl_seconds = event_ttl_seconds
        self._event_stream_maxlen = event_stream_maxlen
        self._command_stream_maxlen = command_stream_maxlen
        self._dispatch_queue_capacity = dispatch_queue_capacity
        self._event_max_bytes = event_max_bytes
        self._command_max_bytes = command_max_bytes
        self._dispatch_stream = f"{self._prefix}:runs"
        self._dispatch_group = f"{self._prefix}:workers"

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

    async def enqueue_run(self, command: Mapping[str, Any]) -> str:
        run_id = str(command["runId"])
        encoded_command = _encode(command)
        if len(encoded_command.encode()) > self._command_max_bytes:
            raise ValueError("run command exceeds the configured Redis size limit")
        result = await self._redis.eval(
            """
            if redis.call('xlen', KEYS[2]) >= tonumber(ARGV[3]) then
              return '__RUN_QUEUE_FULL__'
            end
            if redis.call('set', KEYS[1], '1', 'EX', ARGV[1], 'NX') then
              return redis.call('xadd', KEYS[2], '*', 'command', ARGV[2])
            end
            return ''
            """,
            2,
            self._enqueued_key(run_id),
            self._dispatch_stream,
            self._event_ttl_seconds,
            encoded_command,
            self._dispatch_queue_capacity,
        )
        result = str(result or "")
        if result == "__RUN_QUEUE_FULL__":
            raise RunQueueFullError("the Efferva run queue is full")
        return result

    async def claim_new_runs(
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

    async def reclaim_stale_runs(
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

    async def acknowledge_run(self, dispatch_id: str) -> None:
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xack(
                self._dispatch_stream,
                self._dispatch_group,
                dispatch_id,
            )
            pipeline.xdel(self._dispatch_stream, dispatch_id)
            await pipeline.execute()

    async def touch_run_claim(self, worker_id: str, dispatch_id: str) -> None:
        await self._redis.xclaim(
            self._dispatch_stream,
            self._dispatch_group,
            worker_id,
            min_idle_time=0,
            message_ids=[dispatch_id],
            justid=True,
        )

    async def send_command(self, run_id: str, command: Mapping[str, Any]) -> str:
        key = self._command_stream(run_id)
        encoded_command = _encode(command)
        if len(encoded_command.encode()) > self._command_max_bytes:
            raise ValueError("run command exceeds the configured Redis size limit")
        command_id = await self._redis.xadd(
            key,
            {"command": encoded_command},
            maxlen=self._command_stream_maxlen,
            approximate=True,
        )
        await self._redis.expire(key, self._event_ttl_seconds)
        return str(command_id)

    async def read_commands(
        self,
        run_id: str,
        after: str,
        *,
        block_ms: int = 1000,
    ) -> list[tuple[str, dict[str, Any]]]:
        rows = await self._redis.xread(
            {self._command_stream(run_id): after},
            block=block_ms,
        )
        return [
            (str(entry_id), _decode(fields.get("command")))
            for _, entries in rows
            for entry_id, fields in entries
        ]

    async def publish_event(
        self,
        run_id: str,
        event: Mapping[str, Any],
        *,
        state: Mapping[str, Any] | None = None,
        session_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> str:
        event_key = self._event_stream(run_id)
        state_key = self._state_key(run_id)
        encoded_state = {
            name: _encode(value) for name, value in (state or {}).items()
        }
        encoded_event = _encode(event)
        if len(encoded_event.encode()) > self._event_max_bytes:
            raise ValueError("AG-UI event exceeds the configured Redis size limit")
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xadd(
                event_key,
                {"event": encoded_event},
                maxlen=self._event_stream_maxlen,
                approximate=True,
            )
            pipeline.expire(event_key, self._event_ttl_seconds)
            if encoded_state:
                pipeline.hset(state_key, mapping=encoded_state)
                pipeline.expire(state_key, self._event_ttl_seconds)
            if session_id and thread_id and turn_id:
                pipeline.set(
                    self._turn_key(session_id, thread_id, turn_id),
                    run_id,
                    ex=self._event_ttl_seconds,
                )
            results = await pipeline.execute()
        return str(results[0])

    async def stream_events(
        self,
        run_id: str,
        *,
        after: str = "0-0",
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        cursor = after
        key = self._event_stream(run_id)
        while True:
            rows = await self._redis.xread({key: cursor}, block=15_000)
            if not rows:
                encoded_status = await self._redis.hget(
                    self._state_key(run_id),
                    "status",
                )
                status = _decode(encoded_status) if encoded_status else None
                if status in {"completed", "failed", "interrupted"}:
                    return
                if not await self._redis.exists(
                    key,
                    self._state_key(run_id),
                    self._enqueued_key(run_id),
                ):
                    yield cursor, {
                        "type": "RUN_ERROR",
                        "code": "RUN_EXPIRED",
                        "message": "The run event stream has expired.",
                    }
                    return
                continue
            for _, entries in rows:
                for event_id, fields in entries:
                    cursor = str(event_id)
                    event = _decode(fields.get("event"))
                    yield cursor, event
                    if event.get("type") in {"RUN_FINISHED", "RUN_ERROR"}:
                        return

    async def acquire_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        return bool(
            await self._redis.set(
                self._lease_key(run_id),
                worker_id,
                ex=ttl_seconds,
                nx=True,
            )
        )

    async def renew_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        result = await self._redis.eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
              return redis.call('expire', KEYS[1], ARGV[2])
            end
            return 0
            """,
            1,
            self._lease_key(run_id),
            worker_id,
            ttl_seconds,
        )
        return bool(result)

    async def release_lease(self, run_id: str, worker_id: str) -> None:
        await self._redis.eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
              return redis.call('del', KEYS[1])
            end
            return 0
            """,
            1,
            self._lease_key(run_id),
            worker_id,
        )

    async def acquire_thread_lease(
        self,
        session_id: str,
        thread_id: str,
        run_id: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        return bool(
            await self._redis.set(
                self._thread_lease_key(session_id, thread_id),
                run_id,
                ex=ttl_seconds,
                nx=True,
            )
        )

    async def renew_thread_lease(
        self,
        session_id: str,
        thread_id: str,
        run_id: str,
        *,
        ttl_seconds: int,
    ) -> bool:
        result = await self._redis.eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
              return redis.call('expire', KEYS[1], ARGV[2])
            end
            return 0
            """,
            1,
            self._thread_lease_key(session_id, thread_id),
            run_id,
            ttl_seconds,
        )
        return bool(result)

    async def release_thread_lease(
        self,
        session_id: str,
        thread_id: str,
        run_id: str,
    ) -> None:
        await self._redis.eval(
            """
            if redis.call('get', KEYS[1]) == ARGV[1] then
              return redis.call('del', KEYS[1])
            end
            return 0
            """,
            1,
            self._thread_lease_key(session_id, thread_id),
            run_id,
        )

    async def set_run_state(self, run_id: str, values: Mapping[str, Any]) -> None:
        key = self._state_key(run_id)
        encoded = {name: _encode(value) for name, value in values.items()}
        if encoded:
            await self._redis.hset(key, mapping=encoded)
            await self._redis.expire(key, self._event_ttl_seconds)

    async def get_run_state(self, run_id: str) -> dict[str, Any]:
        values = await self._redis.hgetall(self._state_key(run_id))
        return {name: _decode(value) for name, value in values.items()}

    async def bind_turn(
        self,
        session_id: str,
        thread_id: str,
        turn_id: str,
        run_id: str,
    ) -> None:
        await self._redis.set(
            self._turn_key(session_id, thread_id, turn_id),
            run_id,
            ex=self._event_ttl_seconds,
        )

    async def find_run(
        self,
        session_id: str,
        thread_id: str,
        turn_id: str,
    ) -> str | None:
        value = await self._redis.get(
            self._turn_key(session_id, thread_id, turn_id)
        )
        return str(value) if value else None

    def _event_stream(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}:events"

    def _command_stream(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}:commands"

    def _state_key(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}:state"

    def _lease_key(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}:lease"

    def _turn_key(self, session_id: str, thread_id: str, turn_id: str) -> str:
        return (
            f"{self._prefix}:session:{session_id}:thread:{thread_id}:"
            f"turn:{turn_id}:run"
        )

    def _enqueued_key(self, run_id: str) -> str:
        return f"{self._prefix}:run:{run_id}:enqueued"

    def _thread_lease_key(self, session_id: str, thread_id: str) -> str:
        return f"{self._prefix}:session:{session_id}:thread:{thread_id}:lease"


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
