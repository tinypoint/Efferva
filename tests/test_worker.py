from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from efferva.worker import RunWorker


class _Repository:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.turn_ids: list[str] = []
        self.goals: list[dict[str, object]] = []

    async def run_cancel_requested(self, _run_id) -> bool:
        return False

    async def set_codex_turn_id(self, _run_id, turn_id: str) -> None:
        self.turn_ids.append(turn_id)

    async def set_thread_goal_snapshot(self, _thread_id, goal) -> None:
        self.goals.append(goal)

    async def append_event(self, _run_id, event, **_kwargs) -> None:
        self.events.append(event)


class _Runtime:
    def __init__(self) -> None:
        self.goals = [
            {"status": "active", "objective": "research"},
            {"status": "complete", "objective": "research"},
        ]

    async def get_goal(self, _thread_id: str):
        return self.goals.pop(0)


@pytest.mark.asyncio
async def test_goal_run_waits_for_native_continuation_turns() -> None:
    repository = _Repository()
    runtime = _Runtime()
    worker = RunWorker(
        SimpleNamespace(instance_id="worker"),
        repository,
        runtime,
        None,
    )
    queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    queue.put_nowait(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "codex-thread",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }
    )
    queue.put_nowait(
        {
            "method": "turn/started",
            "params": {
                "threadId": "codex-thread",
                "turn": {"id": "turn-2", "status": "inProgress"},
            },
        }
    )
    queue.put_nowait(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "codex-thread",
                "turn": {"id": "turn-2", "status": "completed"},
            },
        }
    )
    run = {
        "id": uuid4(),
        "thread_id": uuid4(),
        "agui_run_id": "public-run",
        "fencing_epoch": 1,
    }

    await worker._consume_notifications(
        run,
        "codex-thread",
        "turn-1",
        queue,
    )

    assert repository.turn_ids == ["turn-2"]
    assert [event["type"] for event in repository.events] == ["RUN_FINISHED"]
    assert [goal["status"] for goal in repository.goals] == ["active", "complete"]
