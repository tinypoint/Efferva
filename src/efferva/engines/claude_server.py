"""Single-process Claude Agent SDK host copied into each Session Sandbox."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    delete_session,
    get_session_info,
    get_session_messages,
    list_sessions,
    query,
    rename_session,
)
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

WORKSPACE = os.environ["EFFERVA_CLAUDE_WORKSPACE"]
SERVER_TOKEN = os.environ["EFFERVA_CLAUDE_SERVER_TOKEN"]
MODEL = os.environ.get("EFFERVA_CLAUDE_MODEL") or None

app = FastAPI(title="Efferva Claude Code Server")


class MessageRequest(BaseModel):
    threadId: str
    prompt: str


@dataclass(slots=True)
class RequestSink:
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None]
    active: bool = True

    def publish(self, event: str, payload: dict[str, Any]) -> None:
        if not self.active:
            return
        try:
            self.queue.put_nowait((event, payload))
        except asyncio.QueueFull:
            # The transcript remains authoritative; a slow browser may miss deltas.
            pass

    def finish(self) -> None:
        if not self.active:
            return
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            self.queue.get_nowait()
            self.queue.put_nowait(None)


active_tasks: set[asyncio.Task[None]] = set()
active_threads: dict[str, asyncio.Task[None]] = {}


def authorize(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {SERVER_TOKEN}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid capability token")


@app.get("/health")
async def health(
    authorization: str | None = Header(default=None),
) -> dict[str, str]:
    authorize(authorization)
    return {"status": "ok"}


@app.get("/threads")
async def threads(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    authorize(authorization)
    return [asdict(item) for item in list_sessions(directory=WORKSPACE, limit=100)]


@app.get("/threads/{thread_id}")
async def thread(
    thread_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    authorize(authorization)
    info = get_session_info(thread_id, directory=WORKSPACE)
    if info is None:
        raise HTTPException(status_code=404, detail="Claude Thread not found")
    return {
        "thread": asdict(info),
        "messages": [
            _json_value(message)
            for message in get_session_messages(thread_id, directory=WORKSPACE)
        ],
        "active": thread_id in active_threads,
    }


@app.delete("/threads/{thread_id}", response_class=Response)
async def remove_thread(
    thread_id: str,
    authorization: str | None = Header(default=None),
) -> Response:
    authorize(authorization)
    if thread_id in active_threads:
        raise HTTPException(status_code=409, detail="Claude Thread is active")
    if get_session_info(thread_id, directory=WORKSPACE) is None:
        raise HTTPException(status_code=404, detail="Claude Thread not found")
    delete_session(thread_id, directory=WORKSPACE)
    return Response(status_code=204)


@app.post("/messages")
async def messages(
    request: MessageRequest,
    authorization: str | None = Header(default=None),
) -> StreamingResponse:
    authorize(authorization)
    prompt = request.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="prompt cannot be empty")
    thread_id = None if request.threadId == "new" else request.threadId
    if thread_id is not None and thread_id in active_threads:
        raise HTTPException(status_code=409, detail="Claude Thread is active")

    sink = RequestSink(asyncio.Queue(maxsize=1024))
    task = asyncio.create_task(_run_query(prompt, thread_id, sink))
    active_tasks.add(task)
    if thread_id is not None:
        active_threads[thread_id] = task
    task.add_done_callback(active_tasks.discard)

    async def stream():
        try:
            while True:
                item = await sink.queue.get()
                if item is None:
                    return
                event, payload = item
                yield f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        finally:
            sink.active = False

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _run_query(
    prompt: str,
    resumed_thread_id: str | None,
    sink: RequestSink,
) -> None:
    actual_thread_id = resumed_thread_id
    try:
        options = ClaudeAgentOptions(
            cwd=WORKSPACE,
            resume=resumed_thread_id,
            model=MODEL,
            permission_mode="bypassPermissions",
            tools={"type": "preset", "preset": "claude_code"},
            system_prompt={"type": "preset", "preset": "claude_code"},
            setting_sources=["user", "project"],
            include_partial_messages=True,
        )
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, SystemMessage):
                candidate = message.data.get("session_id")
                if isinstance(candidate, str):
                    actual_thread_id = candidate
            elif isinstance(message, (AssistantMessage, ResultMessage)):
                candidate = getattr(message, "session_id", None)
                if isinstance(candidate, str):
                    actual_thread_id = candidate
            if actual_thread_id and actual_thread_id not in active_threads:
                current_task = asyncio.current_task()
                if current_task is not None:
                    active_threads[actual_thread_id] = current_task
            sink.publish("message", _message_value(message))
        if resumed_thread_id is None and actual_thread_id:
            rename_session(
                actual_thread_id,
                _thread_title(prompt),
                directory=WORKSPACE,
            )
        sink.publish("done", {"threadId": actual_thread_id})
    except BaseException as error:
        sink.publish(
            "error",
            {
                "type": type(error).__name__,
                "message": str(error),
                "threadId": actual_thread_id,
            },
        )
    finally:
        task = asyncio.current_task()
        for thread_id, active_task in tuple(active_threads.items()):
            if active_task is task:
                active_threads.pop(thread_id, None)
        sink.finish()


def _message_value(message: Any) -> dict[str, Any]:
    return {
        "type": type(message).__name__,
        "message": _json_value(message),
    }


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _json_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _thread_title(prompt: str) -> str:
    return " ".join(prompt.split())[:80] or "New Thread"
