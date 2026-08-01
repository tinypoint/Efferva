from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from efferva.broker import RedisRunBroker, RunQueueFullError
from efferva.identity import IdentityResolver, Principal
from efferva.models import (
    PrincipalView,
    RunAgentInput,
    RunCreate,
    Session,
    SessionCreate,
    ThreadCreate,
)
from efferva.repository import AccessMode, RunRepository, SessionRepository
from efferva.runtime import CodexProxy


def principal_dependency(
    identity: IdentityResolver,
) -> Callable[[Request], Any]:
    async def resolve(request: Request) -> Principal:
        principal = await identity(request)
        if not isinstance(principal, Principal):
            raise TypeError("IdentityResolver must return efferva.Principal")
        return principal

    return resolve


def _prompt_from_agui(input_payload: RunAgentInput) -> str:
    for message in reversed(input_payload.messages):
        if message.role != "user":
            continue
        if isinstance(message.content, str):
            return message.content
        if isinstance(message.content, list):
            prompt = "".join(
                part.get("text", "")
                for part in message.content
                if part.get("type") in {"text", "input_text"}
            )
            if prompt:
                return prompt
    raise HTTPException(status_code=422, detail="AG-UI input requires a user message")


def _sse(seq: int, event: dict[str, Any]) -> str:
    return f"id: {seq}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


def _sse_event(event_id: str, event: dict[str, Any]) -> str:
    return (
        f"id: {event_id}\n"
        f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
    )


async def _brokered_stream(
    broker: RedisRunBroker,
    run_id: str,
    *,
    after: str = "0-0",
) -> AsyncIterator[str]:
    async for event_id, event in broker.stream_events(run_id, after=after):
        yield _sse_event(event_id, event)


def _brokered_response(
    broker: RedisRunBroker,
    run_id: str,
    *,
    after: str = "0-0",
) -> StreamingResponse:
    return StreamingResponse(
        _brokered_stream(broker, run_id, after=after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_TOOL_ITEM_TYPES = {
    "commandExecution",
    "fileChange",
    "mcpToolCall",
    "dynamicToolCall",
    "collabToolCall",
    "webSearch",
    "imageView",
    "sleep",
}


def _tool_call(item: dict[str, Any]) -> dict[str, Any] | None:
    item_type = item.get("type")
    if item_type not in _TOOL_ITEM_TYPES:
        return None
    tool_call_id = str(item.get("id") or uuid4())
    if item_type == "commandExecution":
        name = "exec_command"
        arguments = {
            "command": item.get("command") or "",
            "cwd": item.get("cwd"),
        }
        result: Any = item.get("aggregatedOutput")
        if result is None:
            result = {
                "status": item.get("status"),
                "exitCode": item.get("exitCode"),
            }
        is_error = item.get("status") in {"failed", "declined"}
    elif item_type == "fileChange":
        name = "apply_patch"
        arguments = {"changes": item.get("changes") or []}
        result = {"status": item.get("status")}
        is_error = item.get("status") in {"failed", "declined"}
    elif item_type == "mcpToolCall":
        server = str(item.get("server") or "mcp")
        tool = str(item.get("tool") or "tool")
        name = f"mcp__{server}__{tool}"
        arguments = item.get("arguments") or {}
        result = item.get("result")
        if result is None:
            result = item.get("error") or {"status": item.get("status")}
        is_error = item.get("status") == "failed" or item.get("error") is not None
    elif item_type == "dynamicToolCall":
        name = str(item.get("tool") or "tool")
        arguments = item.get("arguments") or {}
        result = item.get("contentItems")
        if result is None:
            result = {"status": item.get("status"), "success": item.get("success")}
        is_error = item.get("status") == "failed" or item.get("success") is False
    elif item_type == "collabToolCall":
        name = str(item.get("tool") or "collaboration")
        arguments = {
            "prompt": item.get("prompt"),
            "receiverThreadId": item.get("receiverThreadId"),
        }
        result = {
            "status": item.get("status"),
            "newThreadId": item.get("newThreadId"),
            "agentStatus": item.get("agentStatus"),
        }
        is_error = item.get("status") == "failed"
    elif item_type == "webSearch":
        name = "web_search"
        arguments = {"query": item.get("query"), "action": item.get("action")}
        result = item.get("results") or {"status": item.get("status")}
        is_error = False
    elif item_type == "imageView":
        name = "view_image"
        arguments = {"path": item.get("path")}
        result = {"status": item.get("status") or "completed"}
        is_error = False
    else:
        name = "wait"
        arguments = {"durationMs": item.get("durationMs")}
        result = {"status": item.get("status") or "completed"}
        is_error = False
    result_text = (
        result
        if isinstance(result, str)
        else json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    )
    return {
        "id": tool_call_id,
        "name": name,
        "arguments": json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "result": result_text,
        "is_error": is_error,
    }


def _activity_event(
    notification: dict[str, Any],
) -> dict[str, Any] | None:
    method = str(notification.get("method") or "")
    params = notification.get("params") or {}
    item_id = str(
        params.get("itemId")
        or params.get("turnId")
        or params.get("threadId")
        or uuid4()
    )
    if method == "turn/plan/updated":
        return {
            "type": "ACTIVITY_SNAPSHOT",
            "messageId": f"plan:{item_id}",
            "activityType": "plan",
            "content": {
                "explanation": params.get("explanation"),
                "steps": params.get("plan") or [],
            },
        }
    if method == "turn/diff/updated":
        return {
            "type": "ACTIVITY_SNAPSHOT",
            "messageId": f"diff:{item_id}",
            "activityType": "diff",
            "content": {"diff": params.get("diff") or ""},
        }
    if method == "item/commandExecution/outputDelta":
        return None
    if method == "item/fileChange/patchUpdated":
        return {
            "type": "ACTIVITY_SNAPSHOT",
            "messageId": f"file-change:{item_id}",
            "activityType": "file-change",
            "content": dict(params),
        }
    if method in {"warning", "configWarning", "model/rerouted", "error"}:
        return {
            "type": "ACTIVITY_SNAPSHOT",
            "messageId": f"codex:{method}:{item_id}",
            "activityType": "error" if method == "error" else "notice",
            "content": {"method": method, **dict(params)},
        }
    if method == "thread/tokenUsage/updated":
        return {
            "type": "ACTIVITY_SNAPSHOT",
            "messageId": f"usage:{item_id}",
            "activityType": "usage",
            "content": dict(params),
        }
    return None


async def _agui_stream(
    proxy: CodexProxy,
    session: dict[str, Any],
    thread_id: str,
    prompt: str,
    *,
    run_id: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    collaboration_mode: str | None = None,
    workspace: str | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> AsyncIterator[str]:
    seq = 0
    open_messages: set[str] = set()
    messages_with_delta: set[str] = set()
    open_reasoning: set[str] = set()
    active_thread_id = thread_id
    started_tool_calls: set[str] = set()
    yield _sse(
        seq := seq + 1,
        {
            "type": "RUN_STARTED",
            "runId": run_id,
            "threadId": thread_id,
        },
    )
    yield _sse(
        seq := seq + 1,
        {
            "type": "STATE_SNAPSHOT",
            "snapshot": {
                "threadId": thread_id,
                "runId": run_id,
                "turnId": None,
                "status": "running",
            },
        },
    )
    try:
        notifications = (
            proxy.stream_new_turn(
                session,
                prompt,
                workspace=workspace,
                model=model,
                reasoning_effort=reasoning_effort,
                collaboration_mode=collaboration_mode,
                dynamic_tools=tools,
            )
            if thread_id == "new"
            else proxy.stream_turn(
                session,
                thread_id,
                prompt,
                model=model,
                reasoning_effort=reasoning_effort,
                collaboration_mode=collaboration_mode,
            )
        )
        async for notification in notifications:
            method = notification["method"]
            params = notification.get("params") or {}
            if method == "efferva/thread-created":
                thread = _thread_summary(
                    dict(params["thread"]),
                    UUID(str(session["id"])),
                )
                active_thread_id = str(thread["id"])
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "RAW",
                        "event": {
                            "method": method,
                            "params": {"thread": thread},
                        },
                    },
                )
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "STATE_DELTA",
                        "delta": [
                            {
                                "op": "replace",
                                "path": "/threadId",
                                "value": active_thread_id,
                            }
                        ],
                    },
                )
                continue
            if method == "efferva/turn-started":
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "RAW",
                        "event": notification,
                        "turnId": params.get("turnId"),
                    },
                )
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "STEP_STARTED",
                        "stepName": str(params.get("turnId") or "turn"),
                    },
                )
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "STATE_DELTA",
                        "delta": [
                            {
                                "op": "replace",
                                "path": "/turnId",
                                "value": params.get("turnId"),
                            }
                        ],
                    },
                )
                continue
            if method == "item/agentMessage/delta":
                item_id = str(params.get("itemId") or uuid4())
                message_id = f"{run_id}:{item_id}"
                if message_id not in open_messages:
                    open_messages.add(message_id)
                    yield _sse(
                        seq := seq + 1,
                        {"type": "TEXT_MESSAGE_START", "messageId": message_id},
                    )
                delta = str(params.get("delta") or "")
                if delta:
                    messages_with_delta.add(message_id)
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TEXT_MESSAGE_CONTENT",
                            "messageId": message_id,
                            "delta": delta,
                        },
                    )
                continue
            if method in {
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
            }:
                reasoning_id = f"{run_id}:reasoning:{params.get('itemId') or uuid4()}"
                if reasoning_id not in open_reasoning:
                    open_reasoning.add(reasoning_id)
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "REASONING_MESSAGE_START",
                            "messageId": reasoning_id,
                            "role": "reasoning",
                        },
                    )
                delta = str(params.get("delta") or "")
                if delta:
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "REASONING_MESSAGE_CONTENT",
                            "messageId": reasoning_id,
                            "delta": delta,
                        },
                    )
                continue
            if method == "item/started":
                tool_call = _tool_call(params.get("item") or {})
                if tool_call is not None:
                    tool_call_id = tool_call["id"]
                    started_tool_calls.add(tool_call_id)
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TOOL_CALL_START",
                            "toolCallId": tool_call_id,
                            "toolCallName": tool_call["name"],
                        },
                    )
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TOOL_CALL_ARGS",
                            "toolCallId": tool_call_id,
                            "delta": tool_call["arguments"],
                        },
                    )
                    continue
            if method == "item/completed":
                item = params.get("item") or {}
                if (
                    item.get("type") == "userMessage"
                    and str(item.get("clientId") or "").startswith(
                        "efferva-steer-"
                    )
                ):
                    steered_text = "".join(
                        str(part.get("text") or "")
                        for part in item.get("content") or []
                        if part.get("type") == "text"
                    )
                    if steered_text:
                        steered_message_id = str(item.get("id") or uuid4())
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TEXT_MESSAGE_START",
                                "messageId": steered_message_id,
                                "role": "user",
                            },
                        )
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TEXT_MESSAGE_CONTENT",
                                "messageId": steered_message_id,
                                "delta": steered_text,
                            },
                        )
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TEXT_MESSAGE_END",
                                "messageId": steered_message_id,
                            },
                        )
                    continue
                tool_call = _tool_call(item)
                if tool_call is not None:
                    tool_call_id = tool_call["id"]
                    if tool_call_id not in started_tool_calls:
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TOOL_CALL_START",
                                "toolCallId": tool_call_id,
                                "toolCallName": tool_call["name"],
                            },
                        )
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TOOL_CALL_ARGS",
                                "toolCallId": tool_call_id,
                                "delta": tool_call["arguments"],
                            },
                        )
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TOOL_CALL_END",
                            "toolCallId": tool_call_id,
                        },
                    )
                    started_tool_calls.discard(tool_call_id)
                    result_event = {
                        "type": "TOOL_CALL_RESULT",
                        "messageId": f"{tool_call_id}:result",
                        "toolCallId": tool_call_id,
                        "content": tool_call["result"],
                        "role": "tool",
                    }
                    if tool_call["is_error"]:
                        result_event.update(
                            {
                                "structuredContent": {
                                    "error": tool_call["result"]
                                },
                                "isError": True,
                            }
                        )
                    yield _sse(seq := seq + 1, result_event)
                    continue
                if item.get("type") == "agentMessage":
                    item_id = str(item.get("id") or uuid4())
                    message_id = f"{run_id}:{item_id}"
                    if message_id not in open_messages:
                        open_messages.add(message_id)
                        yield _sse(
                            seq := seq + 1,
                            {"type": "TEXT_MESSAGE_START", "messageId": message_id},
                        )
                    text = str(item.get("text") or "")
                    if text and message_id not in messages_with_delta:
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TEXT_MESSAGE_CONTENT",
                                "messageId": message_id,
                                "delta": text,
                            },
                        )
                    yield _sse(
                        seq := seq + 1,
                        {"type": "TEXT_MESSAGE_END", "messageId": message_id},
                    )
                    open_messages.discard(message_id)
                    continue
                if item.get("type") == "reasoning":
                    reasoning_id = f"{run_id}:reasoning:{item.get('id') or uuid4()}"
                    if reasoning_id in open_reasoning:
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "REASONING_MESSAGE_END",
                                "messageId": reasoning_id,
                            },
                        )
                        open_reasoning.discard(reasoning_id)
                    continue
            if method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status")
                for message_id in tuple(open_messages):
                    yield _sse(
                        seq := seq + 1,
                        {"type": "TEXT_MESSAGE_END", "messageId": message_id},
                    )
                open_messages.clear()
                for reasoning_id in tuple(open_reasoning):
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "REASONING_MESSAGE_END",
                            "messageId": reasoning_id,
                        },
                    )
                open_reasoning.clear()
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "STEP_FINISHED",
                        "stepName": str(turn.get("id") or "turn"),
                    },
                )
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "STATE_DELTA",
                        "delta": [
                            {
                                "op": "replace",
                                "path": "/status",
                                "value": status,
                            }
                        ],
                    },
                )
                if status == "completed":
                    event = {
                        "type": "RUN_FINISHED",
                        "runId": run_id,
                        "threadId": active_thread_id,
                    }
                elif status in {"interrupted", "cancelled"}:
                    event = {
                        "type": "RUN_FINISHED",
                        "runId": run_id,
                        "threadId": active_thread_id,
                        "result": {"status": "interrupted"},
                    }
                else:
                    error = turn.get("error") or {}
                    event = {
                        "type": "RUN_ERROR",
                        "code": "RUNTIME_ERROR",
                        "message": error.get("message") or f"turn {status}",
                    }
                yield _sse(seq := seq + 1, event)
                return
            activity = _activity_event(notification)
            if activity is not None:
                yield _sse(seq := seq + 1, activity)
                continue
            yield _sse(
                seq := seq + 1,
                {"type": "RAW", "event": notification},
            )
    except Exception as error:
        yield _sse(
            seq := seq + 1,
            {
                "type": "RUN_ERROR",
                "code": "RUNTIME_ERROR",
                "message": str(error),
            },
        )


async def _agui_resume_stream(
    proxy: CodexProxy,
    session: dict[str, Any],
    thread_id: str,
    turn_id: str,
    *,
    run_id: str,
    continuation: bool = False,
    open_message_ids: set[str] | None = None,
    open_reasoning_ids: set[str] | None = None,
    started_tool_call_ids: set[str] | None = None,
) -> AsyncIterator[str]:
    seq = 0
    open_messages = set(open_message_ids or ())
    messages_with_delta = set(open_messages) if continuation else set()
    open_reasoning = set(open_reasoning_ids or ())
    started_tool_calls = set(started_tool_call_ids or ())
    if not continuation:
        yield _sse(
            seq := seq + 1,
            {"type": "RUN_STARTED", "runId": run_id, "threadId": thread_id},
        )
        yield _sse(
            seq := seq + 1,
            {
                "type": "STATE_SNAPSHOT",
                "snapshot": {
                    "threadId": thread_id,
                    "runId": run_id,
                    "turnId": turn_id,
                    "status": "running",
                },
            },
        )
    try:
        async for notification in proxy.resume_turn(session, thread_id, turn_id):
            method = notification["method"]
            params = notification.get("params") or {}
            if method == "efferva/thread-resumed":
                resumed_turn = params.get("turn")
                if continuation and params.get("active"):
                    continue
                if continuation and not params.get("active"):
                    resumed_thread = params.get("thread")
                    if resumed_thread:
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "MESSAGES_SNAPSHOT",
                                "messages": _thread_detail(
                                    dict(resumed_thread),
                                    UUID(str(session["id"])),
                                )["messages"],
                            },
                        )
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "RUN_FINISHED",
                            "runId": run_id,
                            "threadId": thread_id,
                        },
                    )
                    return
                resumed_thread = params.get("thread")
                if resumed_thread:
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "MESSAGES_SNAPSHOT",
                            "messages": _thread_detail(
                                dict(resumed_thread),
                                UUID(str(session["id"])),
                            )["messages"],
                        },
                    )
                if not resumed_turn:
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "RUN_FINISHED",
                            "runId": run_id,
                            "threadId": thread_id,
                        },
                    )
                    return
                resumed_turn_id = str(resumed_turn["id"])
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "RAW",
                        "event": {
                            "method": "efferva/turn-started",
                            "params": {
                                "threadId": thread_id,
                                "turnId": resumed_turn_id,
                            },
                        },
                    },
                )
                if params.get("active"):
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "STEP_STARTED",
                            "stepName": resumed_turn_id,
                        },
                    )
                for item in resumed_turn.get("items") or []:
                    if item.get("type") == "agentMessage":
                        message_id = f"{run_id}:{item.get('id') or uuid4()}"
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TEXT_MESSAGE_START",
                                "messageId": message_id,
                            },
                        )
                        text = str(item.get("text") or "")
                        if text:
                            yield _sse(
                                seq := seq + 1,
                                {
                                    "type": "TEXT_MESSAGE_CONTENT",
                                    "messageId": message_id,
                                    "delta": text,
                                },
                            )
                        yield _sse(
                            seq := seq + 1,
                            {"type": "TEXT_MESSAGE_END", "messageId": message_id},
                        )
                        continue
                    tool_call = _tool_call(item)
                    if tool_call is None:
                        continue
                    tool_call_id = tool_call["id"]
                    started_tool_calls.add(tool_call_id)
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TOOL_CALL_START",
                            "toolCallId": tool_call_id,
                            "toolCallName": tool_call["name"],
                        },
                    )
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TOOL_CALL_ARGS",
                            "toolCallId": tool_call_id,
                            "delta": tool_call["arguments"],
                        },
                    )
                    if item.get("status") != "inProgress":
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TOOL_CALL_END",
                                "toolCallId": tool_call_id,
                            },
                        )
                        started_tool_calls.discard(tool_call_id)
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TOOL_CALL_RESULT",
                                "messageId": f"{tool_call_id}:result",
                                "toolCallId": tool_call_id,
                                "content": tool_call["result"],
                                "role": "tool",
                            },
                        )
                if not params.get("active"):
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "RUN_FINISHED",
                            "runId": run_id,
                            "threadId": thread_id,
                        },
                    )
                    return
                continue
            if method == "item/started":
                tool_call = _tool_call(params.get("item") or {})
                if tool_call is not None:
                    tool_call_id = tool_call["id"]
                    started_tool_calls.add(tool_call_id)
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TOOL_CALL_START",
                            "toolCallId": tool_call_id,
                            "toolCallName": tool_call["name"],
                        },
                    )
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TOOL_CALL_ARGS",
                            "toolCallId": tool_call_id,
                            "delta": tool_call["arguments"],
                        },
                    )
                    continue
            if method == "item/agentMessage/delta":
                item_id = str(params.get("itemId") or uuid4())
                message_id = f"{run_id}:{item_id}"
                if message_id not in open_messages:
                    open_messages.add(message_id)
                    yield _sse(
                        seq := seq + 1,
                        {"type": "TEXT_MESSAGE_START", "messageId": message_id},
                    )
                delta = str(params.get("delta") or "")
                if delta:
                    messages_with_delta.add(message_id)
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TEXT_MESSAGE_CONTENT",
                            "messageId": message_id,
                            "delta": delta,
                        },
                    )
                continue
            if method in {
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
            }:
                reasoning_id = f"{run_id}:reasoning:{params.get('itemId') or uuid4()}"
                if reasoning_id not in open_reasoning:
                    open_reasoning.add(reasoning_id)
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "REASONING_MESSAGE_START",
                            "messageId": reasoning_id,
                            "role": "reasoning",
                        },
                    )
                delta = str(params.get("delta") or "")
                if delta:
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "REASONING_MESSAGE_CONTENT",
                            "messageId": reasoning_id,
                            "delta": delta,
                        },
                    )
                continue
            if method == "item/completed":
                item = params.get("item") or {}
                tool_call = _tool_call(item)
                if tool_call is not None:
                    tool_call_id = tool_call["id"]
                    if tool_call_id not in started_tool_calls:
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TOOL_CALL_START",
                                "toolCallId": tool_call_id,
                                "toolCallName": tool_call["name"],
                            },
                        )
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TOOL_CALL_ARGS",
                                "toolCallId": tool_call_id,
                                "delta": tool_call["arguments"],
                            },
                        )
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TOOL_CALL_END",
                            "toolCallId": tool_call_id,
                        },
                    )
                    started_tool_calls.discard(tool_call_id)
                    result_event = {
                        "type": "TOOL_CALL_RESULT",
                        "messageId": f"{tool_call_id}:result",
                        "toolCallId": tool_call_id,
                        "content": tool_call["result"],
                        "role": "tool",
                    }
                    if tool_call["is_error"]:
                        result_event.update(
                            {
                                "structuredContent": {
                                    "error": tool_call["result"]
                                },
                                "isError": True,
                            }
                        )
                    yield _sse(seq := seq + 1, result_event)
                    continue
                if item.get("type") == "agentMessage":
                    item_id = str(item.get("id") or uuid4())
                    message_id = f"{run_id}:{item_id}"
                    if message_id not in open_messages:
                        open_messages.add(message_id)
                        yield _sse(
                            seq := seq + 1,
                            {"type": "TEXT_MESSAGE_START", "messageId": message_id},
                        )
                    text = str(item.get("text") or "")
                    if text and message_id not in messages_with_delta:
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TEXT_MESSAGE_CONTENT",
                                "messageId": message_id,
                                "delta": text,
                            },
                        )
                    yield _sse(
                        seq := seq + 1,
                        {"type": "TEXT_MESSAGE_END", "messageId": message_id},
                    )
                    open_messages.discard(message_id)
                    continue
                if item.get("type") == "reasoning":
                    reasoning_id = f"{run_id}:reasoning:{item.get('id') or uuid4()}"
                    if reasoning_id in open_reasoning:
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "REASONING_MESSAGE_END",
                                "messageId": reasoning_id,
                            },
                        )
                        open_reasoning.discard(reasoning_id)
                continue
            if method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status")
                for message_id in tuple(open_messages):
                    yield _sse(
                        seq := seq + 1,
                        {"type": "TEXT_MESSAGE_END", "messageId": message_id},
                    )
                open_messages.clear()
                for reasoning_id in tuple(open_reasoning):
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "REASONING_MESSAGE_END",
                            "messageId": reasoning_id,
                        },
                    )
                open_reasoning.clear()
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "STEP_FINISHED",
                        "stepName": str(turn.get("id") or turn_id),
                    },
                )
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "STATE_DELTA",
                        "delta": [
                            {
                                "op": "replace",
                                "path": "/status",
                                "value": status,
                            }
                        ],
                    },
                )
                if status == "completed":
                    event = {
                        "type": "RUN_FINISHED",
                        "runId": run_id,
                        "threadId": thread_id,
                    }
                elif status in {"interrupted", "cancelled"}:
                    event = {
                        "type": "RUN_FINISHED",
                        "runId": run_id,
                        "threadId": thread_id,
                        "result": {"status": "interrupted"},
                    }
                else:
                    error = turn.get("error") or {}
                    event = {
                        "type": "RUN_ERROR",
                        "code": "RUNTIME_ERROR",
                        "message": error.get("message") or f"turn {status}",
                    }
                yield _sse(seq := seq + 1, event)
                return
            activity = _activity_event(notification)
            if activity is not None:
                yield _sse(seq := seq + 1, activity)
                continue
            yield _sse(seq := seq + 1, {"type": "RAW", "event": notification})
    except Exception as error:
        yield _sse(
            seq := seq + 1,
            {
                "type": "RUN_ERROR",
                "code": "RUNTIME_ERROR",
                "message": str(error),
            },
        )


def create_api_router(
    *,
    identity: IdentityResolver,
    repository: Callable[[], SessionRepository],
    codex_proxy: Callable[[], CodexProxy],
    run_broker: Callable[[], RedisRunBroker],
    runs: Callable[[], RunRepository],
) -> APIRouter:
    router = APIRouter()
    resolve_principal = principal_dependency(identity)

    async def enqueue(command: dict[str, Any]) -> None:
        run_id = str(command["runId"])
        await runs().create(
            run_id,
            UUID(str(command["sessionId"])),
            str(command["threadId"]),
            command,
        )
        try:
            await run_broker().enqueue_run(command)
        except RunQueueFullError as error:
            await runs().update(run_id, status="failed", error=str(error))
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            await runs().update(run_id, status="failed", error=str(error))
            raise HTTPException(status_code=413, detail=str(error)) from error
        except Exception as error:
            await runs().update(run_id, status="failed", error=str(error))
            raise

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        await repository().ping()
        await run_broker().ping()
        return {"status": "ok"}

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

    @router.get("/api/sessions/{session_id}", response_model=Session)
    async def get_session(
        session_id: UUID,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        return await repository().get_session(principal, session_id)

    @router.get("/api/sessions/{session_id}/threads")
    async def list_threads(
        session_id: UUID,
        principal: Principal = Depends(resolve_principal),
    ) -> list[dict[str, Any]]:
        session = await repository().get_session(principal, session_id, touch=True)
        threads = await codex_proxy().list_threads(session)
        return [_thread_summary(thread, session_id) for thread in threads]

    @router.get("/api/sessions/{session_id}/models")
    async def list_models(
        session_id: UUID,
        principal: Principal = Depends(resolve_principal),
    ) -> list[dict[str, Any]]:
        session = await repository().get_session(principal, session_id, touch=True)
        return await codex_proxy().list_models(session)

    @router.get("/api/sessions/{session_id}/skills")
    async def list_skills(
        session_id: UUID,
        workspace: str | None = Query(default=None),
        principal: Principal = Depends(resolve_principal),
    ) -> list[dict[str, Any]]:
        if workspace is not None and not workspace.startswith("/"):
            raise HTTPException(
                status_code=422,
                detail="workspace must be an absolute Sandbox path",
            )
        session = await repository().get_session(principal, session_id, touch=True)
        return await codex_proxy().list_skills(session, workspace=workspace)

    @router.get("/api/sessions/{session_id}/files")
    async def search_files(
        session_id: UUID,
        query: str = Query(default="", max_length=256),
        workspace: str | None = Query(default=None),
        principal: Principal = Depends(resolve_principal),
    ) -> list[dict[str, Any]]:
        if workspace is not None and not workspace.startswith("/"):
            raise HTTPException(
                status_code=422,
                detail="workspace must be an absolute Sandbox path",
            )
        session = await repository().get_session(principal, session_id, touch=True)
        return await codex_proxy().search_files(
            session,
            query,
            workspace=workspace,
        )

    @router.post("/api/sessions/{session_id}/threads", status_code=201)
    async def create_thread(
        session_id: UUID,
        payload: ThreadCreate,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        thread = await codex_proxy().start_thread(
            session,
            workspace=payload.workspace,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            dynamic_tools=payload.tools,
        )
        return _thread_summary(thread, session_id)

    @router.get("/api/sessions/{session_id}/threads/{thread_id}")
    async def read_thread(
        session_id: UUID,
        thread_id: str,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        session = await repository().get_session(principal, session_id, touch=True)
        thread = await codex_proxy().read_thread(session, thread_id)
        return _thread_detail(thread, session_id)

    @router.get("/api/sessions/{session_id}/threads/{thread_id}/resume")
    async def resume_thread(
        session_id: UUID,
        thread_id: str,
        request: Request,
        turn_id: str = Query(min_length=1),
        principal: Principal = Depends(resolve_principal),
    ) -> StreamingResponse:
        session = await repository().get_session(principal, session_id, touch=True)
        existing = await runs().find_by_turn(session_id, thread_id, turn_id)
        if existing is not None and existing["status"] in {"queued", "running"}:
            after = request.headers.get("last-event-id", "0-0")
            return _brokered_response(run_broker(), str(existing["id"]), after=after)
        run_id = str(uuid4())
        await enqueue(
            {
                "kind": "resume",
                "runId": run_id,
                "sessionId": str(session["id"]),
                "threadId": thread_id,
                "turnId": turn_id,
            }
        )
        return _brokered_response(run_broker(), run_id)

    @router.post("/api/sessions/{session_id}/threads/{thread_id}/turns")
    async def start_turn(
        session_id: UUID,
        thread_id: str,
        payload: RunCreate,
        principal: Principal = Depends(resolve_principal),
    ) -> StreamingResponse:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        run_id = str(uuid4())
        await enqueue(
            {
                "kind": "start",
                "runId": run_id,
                "sessionId": str(session["id"]),
                "threadId": thread_id,
                "prompt": payload.prompt,
                "model": payload.model,
                "reasoningEffort": payload.reasoning_effort,
            }
        )
        return _brokered_response(run_broker(), run_id)

    @router.post(
        "/api/sessions/{session_id}/threads/{thread_id}/turns/{turn_id}/interrupt"
    )
    async def interrupt_turn(
        session_id: UUID,
        thread_id: str,
        turn_id: str,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, bool]:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        run_id = await run_broker().find_run(str(session["id"]), thread_id, turn_id)
        if run_id is None:
            raise HTTPException(status_code=409, detail="active run is not owned by a Worker")
        run = await runs().get(run_id, session_id)
        if run["status"] != "running":
            raise HTTPException(status_code=409, detail="turn is not running")
        await run_broker().send_command(
            run_id,
            {
                "kind": "interrupt",
                "sessionId": str(session["id"]),
                "threadId": thread_id,
                "turnId": turn_id,
            },
        )
        return {"interrupted": True}

    @router.post(
        "/api/sessions/{session_id}/threads/{thread_id}/turns/{turn_id}/steer"
    )
    async def steer_turn(
        session_id: UUID,
        thread_id: str,
        turn_id: str,
        payload: RunCreate,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, str]:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        run_id = await run_broker().find_run(str(session["id"]), thread_id, turn_id)
        if run_id is None:
            raise HTTPException(status_code=409, detail="active run is not owned by a Worker")
        run = await runs().get(run_id, session_id)
        if run["status"] != "running":
            raise HTTPException(status_code=409, detail="turn is not running")
        await run_broker().send_command(
            run_id,
            {
                "kind": "steer",
                "sessionId": str(session["id"]),
                "threadId": thread_id,
                "turnId": turn_id,
                "prompt": payload.prompt,
            },
        )
        return {"turnId": turn_id}

    @router.post("/api/ag-ui")
    async def run_agui(
        payload: RunAgentInput,
        principal: Principal = Depends(resolve_principal),
    ) -> StreamingResponse:
        forwarded = (
            payload.forwarded_props
            if isinstance(payload.forwarded_props, dict)
            else {}
        )
        raw_session_id = forwarded.get("sessionId")
        if not raw_session_id:
            raise HTTPException(
                status_code=422,
                detail="forwardedProps.sessionId is required",
            )
        try:
            session_id = UUID(str(raw_session_id))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="sessionId must be a UUID") from error
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        run_id = str(uuid4())
        client_run_id = payload.run_id or run_id
        prompt = _prompt_from_agui(payload)
        model = str(forwarded.get("model") or "").strip() or None
        reasoning_effort = (
            str(forwarded.get("reasoningEffort") or "").strip() or None
        )
        workspace = str(forwarded.get("workspace") or "").strip() or None
        command, separator, argument = prompt.partition(" ")
        argument = argument.strip() if separator else ""
        if command == "/plan":
            if not argument:
                await enqueue(
                    {
                        "kind": "control",
                        "action": "plan.enable",
                        "runId": run_id,
                        "clientRunId": client_run_id,
                        "sessionId": str(session["id"]),
                        "threadId": payload.thread_id,
                        "model": model,
                        "reasoningEffort": reasoning_effort,
                    }
                )
                return _brokered_response(run_broker(), run_id)
            await enqueue(
                {
                    "kind": "start",
                    "runId": run_id,
                    "clientRunId": client_run_id,
                    "sessionId": str(session["id"]),
                    "threadId": payload.thread_id,
                    "prompt": argument,
                    "model": model,
                    "reasoningEffort": reasoning_effort,
                    "collaborationMode": "plan",
                    "workspace": workspace,
                    "tools": payload.tools,
                }
            )
            return _brokered_response(
                run_broker(),
                run_id,
                after="0-0",
            )
        if command == "/goal":
            action = "goal.get"
            control: dict[str, Any] = {}
            if argument == "clear":
                action = "goal.clear"
            elif argument in {"pause", "resume"}:
                action = "goal.status"
                control["status"] = "paused" if argument == "pause" else "active"
            elif argument:
                action = "goal.set"
                control["objective"] = (
                    argument.removeprefix("edit ").strip()
                    if argument.startswith("edit ")
                    else argument
                )
            await enqueue(
                {
                    "kind": "control",
                    "action": action,
                    "runId": run_id,
                    "clientRunId": client_run_id,
                    "sessionId": str(session["id"]),
                    "threadId": payload.thread_id,
                    **control,
                }
            )
            return _brokered_response(run_broker(), run_id)
        await enqueue(
            {
                "kind": "start",
                "runId": run_id,
                "clientRunId": client_run_id,
                "sessionId": str(session["id"]),
                "threadId": payload.thread_id,
                "prompt": prompt,
                "model": model,
                "reasoningEffort": reasoning_effort,
                "workspace": workspace,
                "tools": payload.tools,
            }
        )
        return _brokered_response(
            run_broker(),
            run_id,
            after="0-0",
        )

    return router


def _thread_summary(thread: dict[str, Any], session_id: UUID) -> dict[str, Any]:
    return {
        "id": str(thread["id"]),
        "session_id": str(session_id),
        "title": thread.get("name") or thread.get("preview") or "Untitled thread",
        "workspace": thread.get("cwd"),
        "status": thread.get("status"),
        "created_at": thread.get("createdAt"),
        "updated_at": thread.get("updatedAt") or thread.get("createdAt"),
    }


def _thread_detail(thread: dict[str, Any], session_id: UUID) -> dict[str, Any]:
    summary = _thread_summary(thread, session_id)
    messages: list[dict[str, Any]] = []
    active_turn = next(
        (
            turn
            for turn in reversed(thread.get("turns") or [])
            if turn.get("status") == "inProgress"
        ),
        None,
    )
    active_turn_id = str(active_turn["id"]) if active_turn is not None else None
    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            item_type = item.get("type")
            if item_type == "userMessage":
                content = item.get("content") or []
                text = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
                messages.append(
                    {"id": item.get("id"), "role": "user", "content": text}
                )
            elif item_type == "agentMessage" and turn.get("id") != active_turn_id:
                messages.append(
                    {
                        "id": item.get("id"),
                        "role": "assistant",
                        "content": item.get("text") or "",
                    }
                )
            elif turn.get("id") != active_turn_id:
                tool_call = _tool_call(item)
                if tool_call is None:
                    continue
                messages.append(
                    {
                        "id": f"{tool_call['id']}:assistant",
                        "role": "assistant",
                        "content": "",
                        "toolCalls": [
                            {
                                "id": tool_call["id"],
                                "type": "function",
                                "function": {
                                    "name": tool_call["name"],
                                    "arguments": tool_call["arguments"],
                                },
                            }
                        ],
                    }
                )
                messages.append(
                    {
                        "id": f"{tool_call['id']}:result",
                        "role": "tool",
                        "name": tool_call["name"],
                        "toolCallId": tool_call["id"],
                        "content": tool_call["result"],
                        "isError": tool_call["is_error"],
                    }
                )
    return {**summary, "messages": messages, "active_turn_id": active_turn_id}
