from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from efferva.codex_projection import (
    TurnTextProjection,
    project_activity,
    project_thread_messages,
    project_tool_call,
)
from efferva.runtime import CodexProxy


def _sse(seq: int, event: dict[str, Any]) -> str:
    return f"id: {seq}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


async def stream_agui_turn(
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
    text_projection = TurnTextProjection(run_id)
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
                thread = dict(params["thread"])
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
                tool_call = project_tool_call(params.get("item") or {})
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
                tool_call = project_tool_call(item)
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
            activity = project_activity(notification)
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


async def resume_agui_turn(
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
    text_projection = TurnTextProjection(
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
                                "messages": project_thread_messages(
                                    dict(resumed_thread)
                                ),
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
                                "messages": project_thread_messages(
                                    dict(resumed_thread)
                                ),
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
                    tool_call = project_tool_call(item)
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
                tool_call = project_tool_call(params.get("item") or {})
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
                tool_call = project_tool_call(item)
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
            activity = project_activity(notification)
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

