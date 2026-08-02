from __future__ import annotations

import json
from typing import Any
from uuid import uuid4


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


def project_tool_call(item: dict[str, Any]) -> dict[str, Any] | None:
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


def project_activity(
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


class TurnTextProjection:
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


def project_turn_messages(
    turns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    active_turn = next(
        (
            turn
            for turn in reversed(turns)
            if turn.get("status") == "inProgress"
        ),
        None,
    )
    active_turn_id = str(active_turn["id"]) if active_turn is not None else None
    for turn in turns:
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
            tool_call = project_tool_call(item)
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
    return messages


def project_thread_messages(thread: dict[str, Any]) -> list[dict[str, Any]]:
    return project_turn_messages(
        [dict(turn) for turn in thread.get("turns") or []]
    )
