from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from uuid import UUID

from efferva.config import Settings
from efferva.events import (
    raw,
    run_cancelled,
    run_error,
    run_finished,
    text_message_content,
    text_message_end,
    text_message_start,
)
from efferva.repository import ConflictError, SystemRepository
from efferva.runtime import CodexRuntime
from efferva.sandbox import SandboxControlPlane, SandboxEnvironment

logger = logging.getLogger(__name__)


class RunWorker:
    def __init__(
        self,
        settings: Settings,
        repository: SystemRepository,
        runtime: CodexRuntime,
        sandboxes: SandboxControlPlane,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._runtime = runtime
        self._sandboxes = sandboxes
        self._stopping = asyncio.Event()
        self._poll_task: asyncio.Task[None] | None = None
        self._lease_task: asyncio.Task[None] | None = None
        self._runs: set[asyncio.Task[None]] = set()

    @property
    def healthy(self) -> bool:
        return (
            self._runtime.healthy
            and self._poll_task is not None
            and not self._poll_task.done()
            and self._lease_task is not None
            and not self._lease_task.done()
        )

    async def start(self) -> None:
        await self._runtime.start()
        await self._repository.requeue_abandoned_runs()
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._lease_task = asyncio.create_task(self._lease_loop())

    async def close(self) -> None:
        self._stopping.set()
        for task in (self._poll_task, self._lease_task):
            if task is not None:
                task.cancel()
        for task in (self._poll_task, self._lease_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        for task in self._runs:
            task.cancel()
        if self._runs:
            await asyncio.gather(*self._runs, return_exceptions=True)
        await self._runtime.close()
        await self._sandboxes.close()

    async def _poll_loop(self) -> None:
        while not self._stopping.is_set():
            if not self._runtime.healthy:
                try:
                    await self._runtime.start()
                except Exception:
                    logger.exception("failed to restart Codex runtime")
                    await asyncio.sleep(self._settings.worker_poll_seconds)
                    continue
            if len(self._runs) >= self._settings.max_parallel_runs_per_instance:
                await asyncio.sleep(self._settings.worker_poll_seconds)
                continue
            claimed = await self._repository.claim_run(
                self._settings.instance_id,
                self._settings.lease_ttl_seconds,
                self._settings.max_parallel_threads_per_session,
            )
            if claimed is None:
                await asyncio.sleep(self._settings.worker_poll_seconds)
                continue
            task = asyncio.create_task(self._process_run(claimed))
            self._runs.add(task)
            task.add_done_callback(self._runs.discard)

    async def _lease_loop(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(self._settings.lease_renew_seconds)
            await self._repository.renew_owned_leases(
                self._settings.instance_id,
                self._settings.lease_ttl_seconds,
            )
            await self._repository.requeue_abandoned_runs()

    async def _process_run(self, run: dict[str, Any]) -> None:
        run_id = run["id"]
        thread_id = run["thread_id"]
        session_id = run["session_id"]
        owner_id = self._settings.instance_id
        epoch = run["fencing_epoch"]
        codex_thread_id = str(run["codex_thread_id"]) if run["codex_thread_id"] else None
        queue: asyncio.Queue[dict[str, Any]] | None = None
        try:
            sandbox = await self._sandboxes.ensure(
                session_id,
                run["workspace_ref"],
                owner_id=owner_id,
                fencing_token=epoch,
            )
            await self._runtime.ensure_environment(sandbox)
            runtime_config = dict(run.get("runtime_config_json") or {})
            if codex_thread_id is None:
                codex_thread_id = await self._runtime.start_thread(sandbox, runtime_config)
                await self._repository.set_codex_thread_id(thread_id, UUID(codex_thread_id))
            else:
                await self._resume_with_retry(codex_thread_id, sandbox, runtime_config)

            await self._runtime.set_memory_mode(
                codex_thread_id,
                str(runtime_config.get("memory_mode", "disabled")),
            )

            self._runtime.bind_run_context(codex_thread_id, run)
            queue = self._runtime.subscribe(codex_thread_id)
            run_input = run.get("input") or {}
            turn_id = await self._runtime.start_turn(
                codex_thread_id,
                run["prompt"],
                sandbox,
                model=run_input.get("model"),
                reasoning_effort=run_input.get("reasoning_effort"),
            )
            await self._repository.set_codex_turn_id(run_id, turn_id)
            stored_goal = run.get("goal_json")
            if isinstance(stored_goal, dict):
                native_goal = await self._runtime.set_goal(
                    codex_thread_id,
                    objective=stored_goal.get("objective"),
                    status=stored_goal.get("status"),
                    token_budget=stored_goal.get(
                        "tokenBudget",
                        stored_goal.get("token_budget"),
                    ),
                )
                await self._repository.set_thread_goal_snapshot(thread_id, native_goal)
            await self._consume_notifications(run, codex_thread_id, turn_id, queue)
            goal = await self._runtime.get_goal(codex_thread_id)
            if goal is not None:
                await self._repository.set_thread_goal_snapshot(thread_id, goal)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("run %s failed", run_id)
            with contextlib.suppress(ConflictError):
                await self._append(
                    run_id,
                    run_error(str(error)),
                    owner_id,
                    epoch,
                )
        finally:
            if queue is not None and codex_thread_id is not None:
                self._runtime.unsubscribe(codex_thread_id)
                with contextlib.suppress(Exception):
                    await self._runtime.unload_thread(codex_thread_id)
            with contextlib.suppress(Exception):
                await self._repository.release_session_if_idle(session_id, owner_id)

    async def _resume_with_retry(
        self,
        thread_id: str,
        sandbox: SandboxEnvironment,
        runtime_config: dict[str, Any],
    ) -> None:
        deadline = asyncio.get_running_loop().time() + self._settings.lease_ttl_seconds + 5
        while True:
            try:
                await self._runtime.resume_thread(thread_id, sandbox, runtime_config)
                return
            except Exception:
                if asyncio.get_running_loop().time() >= deadline:
                    raise
                await asyncio.sleep(1)

    async def _consume_notifications(
        self,
        run: dict[str, Any],
        codex_thread_id: str,
        turn_id: str,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        run_id: UUID = run["id"]
        owner_id = self._settings.instance_id
        epoch = run["fencing_epoch"]
        public_run_id = run["agui_run_id"]
        message_id: str | None = None
        message_has_content = False
        interrupt_sent = False
        active_turn_id = turn_id
        while True:
            try:
                notification = await asyncio.wait_for(queue.get(), timeout=0.5)
            except TimeoutError:
                if (
                    not interrupt_sent
                    and await self._repository.run_cancel_requested(run_id)
                ):
                    await self._runtime.interrupt_turn(codex_thread_id, active_turn_id)
                    interrupt_sent = True
                continue
            method = notification["method"]
            params = notification.get("params") or {}
            notification_turn_id = params.get("turnId") or (params.get("turn") or {}).get("id")
            if method == "turn/started" and notification_turn_id is not None:
                active_turn_id = str(notification_turn_id)
                await self._repository.set_codex_turn_id(run_id, active_turn_id)
                interrupt_sent = False
            elif (
                notification_turn_id is not None
                and str(notification_turn_id) != active_turn_id
            ):
                continue

            if method == "efferva/runtimeError":
                raise RuntimeError(params.get("message") or "Codex runtime stopped")

            if method == "item/agentMessage/delta":
                item_id = str(params["itemId"])
                if message_id is None:
                    message_id = f"{public_run_id}:{item_id}"
                    await self._append(
                        run_id,
                        text_message_start(message_id),
                        owner_id,
                        epoch,
                    )
                delta = params.get("delta", "")
                if delta:
                    message_has_content = True
                    await self._append(
                        run_id,
                        text_message_content(message_id, delta),
                        owner_id,
                        epoch,
                    )
                continue

            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    item_id = str(item["id"])
                    if message_id is None:
                        message_id = f"{public_run_id}:{item_id}"
                        await self._append(
                            run_id,
                            text_message_start(message_id),
                            owner_id,
                            epoch,
                        )
                    text = item.get("text", "")
                    if text and not message_has_content:
                        await self._append(
                            run_id,
                            text_message_content(message_id, text),
                            owner_id,
                            epoch,
                        )
                    await self._append(
                        run_id,
                        text_message_end(message_id),
                        owner_id,
                        epoch,
                    )
                    message_id = None
                    message_has_content = False
                else:
                    await self._append(run_id, raw(notification), owner_id, epoch)
                continue

            if method == "thread/goal/updated":
                goal = params.get("goal")
                if isinstance(goal, dict):
                    await self._repository.set_thread_goal_snapshot(
                        run["thread_id"],
                        goal,
                    )
                await self._append(run_id, raw(notification), owner_id, epoch)
                continue

            if method == "turn/completed":
                turn = params.get("turn") or {}
                if message_id is not None:
                    await self._append(
                        run_id,
                        text_message_end(message_id),
                        owner_id,
                        epoch,
                    )
                if interrupt_sent or await self._repository.run_cancel_requested(run_id):
                    event = run_cancelled()
                elif turn.get("status") == "completed":
                    goal = await self._runtime.get_goal(codex_thread_id)
                    if goal is not None:
                        await self._repository.set_thread_goal_snapshot(
                            run["thread_id"],
                            goal,
                        )
                    if goal is not None and goal.get("status") == "active":
                        message_id = None
                        message_has_content = False
                        continue
                    event = run_finished(run["thread_id"], public_run_id)
                else:
                    error = turn.get("error") or {}
                    event = run_error(error.get("message") or f"turn {turn.get('status')}")
                await self._append(run_id, event, owner_id, epoch)
                return

            if method in {
                "item/started",
                "turn/diff/updated",
                "turn/plan/updated",
                "item/commandExecution/outputDelta",
                "item/fileChange/patchUpdated",
                "thread/goal/cleared",
                "thread/compacted",
            }:
                await self._append(run_id, raw(notification), owner_id, epoch)

    async def _append(
        self,
        run_id: UUID,
        event: dict[str, Any],
        owner_id: str,
        epoch: int,
    ) -> None:
        await self._repository.append_event(
            run_id,
            event,
            owner_id=owner_id,
            fencing_epoch=epoch,
        )
