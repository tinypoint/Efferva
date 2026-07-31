from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID


def timestamp_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def run_started(thread_id: UUID, run_id: str, input_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "RUN_STARTED",
        "threadId": str(thread_id),
        "runId": run_id,
        "input": input_payload,
        "timestamp": timestamp_ms(),
    }


def run_finished(thread_id: UUID, run_id: str) -> dict[str, Any]:
    return {
        "type": "RUN_FINISHED",
        "threadId": str(thread_id),
        "runId": run_id,
        "timestamp": timestamp_ms(),
    }


def run_error(message: str, code: str = "RUNTIME_ERROR") -> dict[str, Any]:
    return {
        "type": "RUN_ERROR",
        "message": message,
        "code": code,
        "timestamp": timestamp_ms(),
    }


def run_cancelled(message: str = "Run cancelled by user") -> dict[str, Any]:
    return {
        "type": "RUN_CANCELLED",
        "message": message,
        "timestamp": timestamp_ms(),
    }


def text_message_start(message_id: str) -> dict[str, Any]:
    return {
        "type": "TEXT_MESSAGE_START",
        "messageId": message_id,
        "role": "assistant",
        "timestamp": timestamp_ms(),
    }


def text_message_content(message_id: str, delta: str) -> dict[str, Any]:
    return {
        "type": "TEXT_MESSAGE_CONTENT",
        "messageId": message_id,
        "delta": delta,
        "timestamp": timestamp_ms(),
    }


def text_message_end(message_id: str) -> dict[str, Any]:
    return {
        "type": "TEXT_MESSAGE_END",
        "messageId": message_id,
        "timestamp": timestamp_ms(),
    }


def raw(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "RAW",
        "event": event,
        "source": "codex-app-server",
        "timestamp": timestamp_ms(),
    }


TERMINAL_EVENTS = {"RUN_FINISHED", "RUN_ERROR", "RUN_CANCELLED"}
