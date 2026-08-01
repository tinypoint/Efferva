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
    ExecutionSettings,
    ExecutionSettingsUpdate,
    PrincipalView,
    RunAgentInput,
    RunCreate,
    Session,
    SessionCreate,
    ThreadCreate,
)
from efferva.repository import (
    AccessMode,
    RunRepository,
    SessionDefaultsRepository,
    SessionRepository,
)
from efferva.runtime import CodexProxy, CodexRpcError


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
            if any(
                part.get("type") in {"image", "audio"}
                for part in message.content
            ):
                return ""
    raise HTTPException(status_code=422, detail="AG-UI input requires a user message")


def _media_inputs_from_agui(input_payload: RunAgentInput) -> list[dict[str, Any]]:
    for message in reversed(input_payload.messages):
        if message.role != "user" or not isinstance(message.content, list):
            continue
        inputs: list[dict[str, Any]] = []
        for part in message.content:
            media_type = str(part.get("type") or "")
            if media_type not in {"image", "audio"}:
                continue
            source = part.get("source")
            if not isinstance(source, dict):
                continue
            value = str(source.get("value") or "")
            if not value:
                continue
            if source.get("type") == "data":
                mime_type = str(source.get("mimeType") or "application/octet-stream")
                value = f"data:{mime_type};base64,{value}"
            inputs.append({"type": media_type, "url": value})
        return inputs
    return []


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


class _TurnTextProjection:
    """Project Codex turn text into one live process and one final reply."""

    def __init__(self, run_id: str, *, process_open: bool = False) -> None:
        self._run_id = run_id
        self.process_message_id = f"{run_id}:process"
        self._process_open = process_open
        self._process_has_content = process_open
        self._process_items: set[str] = set()
        self._agent_text: dict[str, str] = {}
        self._last_agent_id: str | None = None

    def _append_process(self, item_key: str, delta: str) -> list[dict[str, Any]]:
        if not delta:
            return []
        events: list[dict[str, Any]] = []
        if not self._process_open:
            self._process_open = True
            events.append(
                {
                    "type": "REASONING_MESSAGE_START",
                    "messageId": self.process_message_id,
                    "role": "reasoning",
                }
            )
        if item_key not in self._process_items:
            self._process_items.add(item_key)
            if self._process_has_content:
                events.append(
                    {
                        "type": "REASONING_MESSAGE_CONTENT",
                        "messageId": self.process_message_id,
                        "delta": "\n\n",
                    }
                )
        events.append(
            {
                "type": "REASONING_MESSAGE_CONTENT",
                "messageId": self.process_message_id,
                "delta": delta,
            }
        )
        self._process_has_content = True
        return events

    def append_agent_delta(
        self,
        item_id: str,
        delta: str,
    ) -> list[dict[str, Any]]:
        if not delta:
            return []
        self._agent_text[item_id] = self._agent_text.get(item_id, "") + delta
        self._last_agent_id = item_id
        return self._append_process(f"agent:{item_id}", delta)

    def complete_agent(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        item_id = str(item.get("id") or uuid4())
        text = str(item.get("text") or "")
        streamed = self._agent_text.get(item_id, "")
        events: list[dict[str, Any]] = []
        if text and not streamed:
            events = self._append_process(f"agent:{item_id}", text)
        elif text.startswith(streamed) and len(text) > len(streamed):
            events = self._append_process(
                f"agent:{item_id}",
                text[len(streamed) :],
            )
        self._agent_text[item_id] = text or streamed
        self._last_agent_id = item_id
        return events

    def seed_agent(self, item: dict[str, Any]) -> None:
        item_id = str(item.get("id") or uuid4())
        self._agent_text[item_id] = str(item.get("text") or "")
        self._last_agent_id = item_id

    def append_reasoning_delta(
        self,
        item_id: str,
        delta: str,
    ) -> list[dict[str, Any]]:
        return self._append_process(f"reasoning:{item_id}", delta)

    def close_process(self) -> list[dict[str, Any]]:
        if not self._process_open:
            return []
        self._process_open = False
        return [
            {
                "type": "REASONING_MESSAGE_END",
                "messageId": self.process_message_id,
            }
        ]

    def finish(self) -> list[dict[str, Any]]:
        events = self.close_process()
        final_text = (
            self._agent_text.get(self._last_agent_id, "")
            if self._last_agent_id is not None
            else ""
        )
        if not final_text:
            return events
        message_id = f"{self._run_id}:assistant"
        events.extend(
            [
                {"type": "TEXT_MESSAGE_START", "messageId": message_id},
                {
                    "type": "TEXT_MESSAGE_CONTENT",
                    "messageId": message_id,
                    "delta": final_text,
                },
                {"type": "TEXT_MESSAGE_END", "messageId": message_id},
            ]
        )
        return events


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
    inputs: list[dict[str, Any]] | None = None,
) -> AsyncIterator[str]:
    seq = 0
    text_projection = _TurnTextProjection(run_id)
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
                "activities": {},
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
                extra_inputs=inputs,
            )
            if thread_id == "new"
            else proxy.stream_turn(
                session,
                thread_id,
                prompt,
                model=model,
                reasoning_effort=reasoning_effort,
                collaboration_mode=collaboration_mode,
                extra_inputs=inputs,
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
                started_at = params.get("startedAt")
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
                            },
                            *(
                                [
                                    {
                                        "op": "add",
                                        "path": "/startedAt",
                                        "value": started_at,
                                    }
                                ]
                                if started_at is not None
                                else []
                            ),
                        ],
                    },
                )
                continue
            if method == "item/agentMessage/delta":
                item_id = str(params.get("itemId") or uuid4())
                delta = str(params.get("delta") or "")
                for event in text_projection.append_agent_delta(item_id, delta):
                    yield _sse(
                        seq := seq + 1,
                        event,
                    )
                continue
            if method in {
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
            }:
                item_id = str(params.get("itemId") or uuid4())
                delta = str(params.get("delta") or "")
                for event in text_projection.append_reasoning_delta(item_id, delta):
                    yield _sse(
                        seq := seq + 1,
                        event,
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
                    for event in text_projection.complete_agent(item):
                        yield _sse(seq := seq + 1, event)
                    continue
                if item.get("type") == "reasoning":
                    continue
            if method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status")
                for event in text_projection.finish():
                    yield _sse(seq := seq + 1, event)
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
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "STATE_DELTA",
                        "delta": [
                            {
                                "op": "add",
                                "path": f"/activities/{activity['activityType']}",
                                "value": activity["content"],
                            }
                        ],
                    },
                )
                continue
            yield _sse(
                seq := seq + 1,
                {"type": "RAW", "event": notification},
            )
    except Exception as error:
        for event in text_projection.close_process():
            yield _sse(seq := seq + 1, event)
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
    legacy_open_messages = set(open_message_ids or ())
    persisted_open_reasoning = set(open_reasoning_ids or ())
    process_message_id = f"{run_id}:process"
    text_projection = _TurnTextProjection(
        run_id,
        process_open=process_message_id in persisted_open_reasoning,
    )
    legacy_open_reasoning = persisted_open_reasoning - {process_message_id}
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
                    "activities": {},
                },
            },
        )
    try:
        async for notification in proxy.resume_turn(session, thread_id, turn_id):
            method = notification["method"]
            params = notification.get("params") or {}
            if method == "efferva/thread-resumed":
                resumed_turn = params.get("turn")
                active = bool(params.get("active"))
                if continuation and active:
                    for item in (resumed_turn or {}).get("items") or []:
                        if item.get("type") == "agentMessage":
                            text_projection.seed_agent(item)
                    continue
                if continuation and not active:
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
                if not resumed_turn or not active:
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
                                "startedAt": resumed_turn.get("startedAt"),
                            },
                        },
                    },
                )
                if resumed_turn.get("startedAt") is not None:
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "STATE_DELTA",
                            "delta": [
                                {
                                    "op": "add",
                                    "path": "/startedAt",
                                    "value": resumed_turn.get("startedAt"),
                                }
                            ],
                        },
                    )
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "STEP_STARTED",
                        "stepName": resumed_turn_id,
                    },
                )
                for item in resumed_turn.get("items") or []:
                    if item.get("type") == "agentMessage":
                        for event in text_projection.complete_agent(item):
                            yield _sse(seq := seq + 1, event)
                        continue
                    if item.get("type") == "reasoning":
                        text = "\n\n".join(
                            str(part)
                            for part in [
                                *(item.get("summary") or []),
                                *(item.get("content") or []),
                            ]
                            if str(part).strip()
                        )
                        for event in text_projection.append_reasoning_delta(
                            str(item.get("id") or uuid4()),
                            text,
                        ):
                            yield _sse(seq := seq + 1, event)
                        continue
                    if item.get("type") == "plan":
                        text = str(item.get("text") or "").strip()
                        for event in text_projection.append_reasoning_delta(
                            str(item.get("id") or uuid4()),
                            f"计划\n\n{text}" if text else "",
                        ):
                            yield _sse(seq := seq + 1, event)
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
                delta = str(params.get("delta") or "")
                for event in text_projection.append_agent_delta(item_id, delta):
                    yield _sse(seq := seq + 1, event)
                continue
            if method in {
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
            }:
                item_id = str(params.get("itemId") or uuid4())
                delta = str(params.get("delta") or "")
                for event in text_projection.append_reasoning_delta(item_id, delta):
                    yield _sse(seq := seq + 1, event)
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
                    for event in text_projection.complete_agent(item):
                        yield _sse(seq := seq + 1, event)
                    continue
                if item.get("type") == "reasoning":
                    continue
                continue
            if method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status")
                for message_id in tuple(legacy_open_messages):
                    yield _sse(
                        seq := seq + 1,
                        {"type": "TEXT_MESSAGE_END", "messageId": message_id},
                    )
                legacy_open_messages.clear()
                for reasoning_id in tuple(legacy_open_reasoning):
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "REASONING_MESSAGE_END",
                            "messageId": reasoning_id,
                        },
                    )
                legacy_open_reasoning.clear()
                for event in text_projection.finish():
                    yield _sse(seq := seq + 1, event)
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
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "STATE_DELTA",
                        "delta": [
                            {
                                "op": "add",
                                "path": f"/activities/{activity['activityType']}",
                                "value": activity["content"],
                            }
                        ],
                    },
                )
                continue
            yield _sse(seq := seq + 1, {"type": "RAW", "event": notification})
    except Exception as error:
        for message_id in tuple(legacy_open_messages):
            yield _sse(
                seq := seq + 1,
                {"type": "TEXT_MESSAGE_END", "messageId": message_id},
            )
        for reasoning_id in tuple(legacy_open_reasoning):
            yield _sse(
                seq := seq + 1,
                {
                    "type": "REASONING_MESSAGE_END",
                    "messageId": reasoning_id,
                },
            )
        for event in text_projection.close_process():
            yield _sse(seq := seq + 1, event)
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
    session_defaults: Callable[[], SessionDefaultsRepository],
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

    async def validate_execution_settings(
        session: dict[str, Any],
        model: str,
        reasoning_effort: str | None = None,
    ) -> None:
        models = await codex_proxy().list_models(session)
        selected = next(
            (item for item in models if item.get("model") == model),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=422, detail="unsupported model")
        if reasoning_effort is None:
            return
        efforts = {
            str(item.get("reasoningEffort"))
            for item in selected.get("supportedReasoningEfforts") or []
            if item.get("reasoningEffort")
        }
        if reasoning_effort not in efforts:
            raise HTTPException(
                status_code=422,
                detail="unsupported reasoning effort for this model",
            )

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

    @router.get(
        "/api/sessions/{session_id}/settings",
        response_model=ExecutionSettings,
    )
    async def get_session_execution_settings(
        session_id: UUID,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, str | None]:
        await repository().get_session(principal, session_id)
        return await session_defaults().get_session(session_id)

    @router.put(
        "/api/sessions/{session_id}/settings",
        response_model=ExecutionSettings,
    )
    async def update_session_execution_settings(
        session_id: UUID,
        payload: ExecutionSettingsUpdate,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, str]:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
        )
        await validate_execution_settings(
            session,
            payload.model,
            payload.reasoning_effort,
        )
        return await session_defaults().set_session(
            session_id,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
        )

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
        defaults = await session_defaults().get_session(session_id)
        model = payload.model or defaults["model"]
        reasoning_effort = (
            payload.reasoning_effort or defaults["reasoning_effort"]
        )
        if model:
            await validate_execution_settings(session, model, reasoning_effort)
        thread = await codex_proxy().start_thread(
            session,
            workspace=payload.workspace,
            model=model,
            reasoning_effort=reasoning_effort,
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
        try:
            thread = await codex_proxy().read_thread(session, thread_id)
        except CodexRpcError as error:
            message = str(error.error.get("message") or "").lower()
            if "not found" in message or "no rollout found" in message:
                raise HTTPException(
                    status_code=404,
                    detail=f"thread {thread_id} not found",
                ) from error
            raise
        detail = _thread_detail(thread, session_id)
        active_turn_id = detail.get("active_turn_id")
        if active_turn_id:
            active_run = await runs().find_by_turn(
                session_id,
                thread_id,
                str(active_turn_id),
            )
            if active_run is not None and active_run.get("started_at") is not None:
                detail["active_turn_started_at"] = active_run["started_at"].isoformat()
        latest_run = await runs().find_latest_for_thread(session_id, thread_id)
        if (
            latest_run is not None
            and latest_run.get("status") == "failed"
        ):
            detail["last_run_error"] = str(
                latest_run.get("error") or "Run failed"
            )
        return detail

    @router.get(
        "/api/sessions/{session_id}/threads/{thread_id}/settings",
        response_model=ExecutionSettings,
    )
    async def get_thread_execution_settings(
        session_id: UUID,
        thread_id: str,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, str | None]:
        session = await repository().get_session(principal, session_id)
        return await codex_proxy().get_thread_settings(session, thread_id)

    @router.put(
        "/api/sessions/{session_id}/threads/{thread_id}/settings",
        response_model=ExecutionSettings,
    )
    async def update_thread_execution_settings(
        session_id: UUID,
        thread_id: str,
        payload: ExecutionSettingsUpdate,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, str]:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
        )
        await validate_execution_settings(
            session,
            payload.model,
            payload.reasoning_effort,
        )
        return await codex_proxy().update_thread_settings(
            session,
            thread_id,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
        )

    @router.delete("/api/sessions/{session_id}/threads/{thread_id}")
    async def delete_thread(
        session_id: UUID,
        thread_id: str,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, bool]:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        await codex_proxy().delete_thread(session, thread_id)
        return {"deleted": True}

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
        if existing is not None and existing["status"] in {
            "queued",
            "running",
            "waiting_input",
        }:
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
        if payload.model:
            await validate_execution_settings(
                session,
                payload.model,
                payload.reasoning_effort,
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
        if payload.resume is not None:
            pending = await run_broker().get_pending_interrupt(
                str(session["id"]),
                payload.thread_id,
            )
            if pending is None:
                raise HTTPException(
                    status_code=409,
                    detail="this thread has no pending Codex interrupt",
                )
            pending_run_id = str(pending["runId"])
            run = await runs().get(pending_run_id, session_id)
            if run["status"] not in {"running", "waiting_input"}:
                raise HTTPException(
                    status_code=409,
                    detail="the interrupted Codex run is no longer active",
                )
            await run_broker().send_command(
                pending_run_id,
                {"kind": "resume_interrupt", "responses": payload.resume},
            )
            return _brokered_response(
                run_broker(),
                pending_run_id,
                after=str(pending["eventId"]),
            )
        run_id = str(uuid4())
        client_run_id = payload.run_id or run_id
        prompt = _prompt_from_agui(payload)
        model = str(forwarded.get("model") or "").strip() or None
        reasoning_effort = (
            str(forwarded.get("reasoningEffort") or "").strip() or None
        )
        if payload.thread_id == "new":
            defaults = await session_defaults().get_session(session_id)
            model = model or defaults["model"]
            reasoning_effort = reasoning_effort or defaults["reasoning_effort"]
        if model:
            await validate_execution_settings(session, model, reasoning_effort)
        if payload.thread_id == "new" and model and reasoning_effort:
            await session_defaults().set_session(
                session_id,
                model=model,
                reasoning_effort=reasoning_effort,
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
                "inputs": _media_inputs_from_agui(payload),
            }
        )
        return _brokered_response(
            run_broker(),
            run_id,
            after="0-0",
        )

    return router


def _thread_summary(thread: dict[str, Any], session_id: UUID) -> dict[str, Any]:
    name = thread.get("name")
    preview = thread.get("preview")
    return {
        "id": str(thread["id"]),
        "session_id": str(session_id),
        "name": name,
        "preview": preview,
        "title": name or "Untitled thread",
        "workspace": thread.get("cwd"),
        "status": thread.get("status"),
        "created_at": thread.get("createdAt"),
        "updated_at": thread.get("updatedAt") or thread.get("createdAt"),
    }


def _user_message_content(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    text = "".join(
        str(part.get("text") or "")
        for part in parts
        if part.get("type") == "text"
    )
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    for part in parts:
        part_type = part.get("type")
        if part_type not in {"image", "audio"}:
            continue
        url = str(part.get("url") or "")
        if not url:
            continue
        content.append(
            {
                "type": part_type,
                "source": {"type": "url", "value": url},
            }
        )
    return content if any(part.get("type") != "text" for part in content) else text


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
        items = list(turn.get("items") or [])
        for item in items:
            if item.get("type") != "userMessage":
                continue
            content = item.get("content") or []
            messages.append(
                {
                    "id": item.get("id"),
                    "role": "user",
                    "content": _user_message_content(content),
                }
            )
        if turn.get("id") == active_turn_id:
            continue
        final_message = next(
            (
                item
                for item in reversed(items)
                if item.get("type") == "agentMessage"
            ),
            None,
        )
        tool_calls: list[dict[str, Any]] = []
        process: list[dict[str, Any]] = []
        for item in items:
            if item.get("type") == "agentMessage" and item is not final_message:
                text = str(item.get("text") or "")
                if text:
                    process.append({"type": "reasoning", "text": text})
                continue
            if item.get("type") == "reasoning":
                text = "\n\n".join(
                    str(part)
                    for part in [
                        *(item.get("summary") or []),
                        *(item.get("content") or []),
                    ]
                    if str(part).strip()
                )
                if text:
                    process.append({"type": "reasoning", "text": text})
                continue
            if item.get("type") == "plan":
                text = str(item.get("text") or "").strip()
                if text:
                    process.append(
                        {"type": "reasoning", "text": f"计划\n\n{text}"}
                    )
                continue
            tool_call = _tool_call(item)
            if tool_call is not None:
                tool_calls.append(tool_call)
                process.append(
                    {"type": "tool-call", "toolCallId": tool_call["id"]}
                )
        if final_message is not None or tool_calls:
            assistant_id = (
                final_message.get("id")
                if final_message is not None
                else f"{turn.get('id')}:assistant"
            )
            assistant_message: dict[str, Any] = {
                "id": assistant_id,
                "role": "assistant",
                "content": (
                    final_message.get("text") or ""
                    if final_message is not None
                    else ""
                ),
            }
            if tool_calls:
                assistant_message["toolCalls"] = [
                    {
                        "id": tool_call["id"],
                        "type": "function",
                        "function": {
                            "name": tool_call["name"],
                            "arguments": tool_call["arguments"],
                        },
                    }
                    for tool_call in tool_calls
                ]
            if process:
                assistant_message["process"] = process
                duration_ms = turn.get("durationMs")
                if not isinstance(duration_ms, (int, float)):
                    started_at = turn.get("startedAt")
                    completed_at = turn.get("completedAt")
                    if isinstance(started_at, (int, float)) and isinstance(
                        completed_at, (int, float)
                    ):
                        duration_ms = max(0, (completed_at - started_at) * 1000)
                if isinstance(duration_ms, (int, float)):
                    assistant_message["processDurationMs"] = round(duration_ms)
            messages.append(assistant_message)
        for tool_call in tool_calls:
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
