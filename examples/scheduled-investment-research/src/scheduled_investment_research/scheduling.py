from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from efferva import SandboxProvider
from efferva.db import Database
from scheduled_investment_research.reports import (
    REPORT_MODEL,
    REPORT_REASONING_EFFORT,
    execute_report,
    mark_interrupted_runs_failed,
    normalize_report_filename,
    notify_run_changed,
)


logger = logging.getLogger(__name__)


class ReportTaskCreate(BaseModel):
    report_type: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=240)
    filename: str = Field(min_length=1, max_length=240)
    prompt: str = Field(min_length=1, max_length=40_000)
    cron_expression: str = Field(min_length=1, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    model: str = Field(default=REPORT_MODEL, min_length=1, max_length=120)
    reasoning_effort: str = Field(
        default=REPORT_REASONING_EFFORT,
        min_length=1,
        max_length=40,
    )
    enabled: bool = True


class ReportTaskUpdate(BaseModel):
    report_type: str | None = Field(default=None, min_length=1, max_length=120)
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    filename: str | None = Field(default=None, min_length=1, max_length=240)
    prompt: str | None = Field(default=None, min_length=1, max_length=40_000)
    cron_expression: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, min_length=1, max_length=120)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=40)
    enabled: bool | None = None


class ReportTask(BaseModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
    owner_user_id: str
    report_type: str
    subject: str
    title: str
    filename: str
    prompt: str
    cron_expression: str
    timezone: str
    model: str
    reasoning_effort: str
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None


@dataclass(frozen=True, slots=True)
class _ScheduledRun:
    id: UUID
    owner_user_id: str
    report_type: str
    subject: str
    title: str
    filename: str
    prompt: str
    model: str
    reasoning_effort: str


_TASK_COLUMNS = """
    id,
    created_at,
    updated_at,
    owner_user_id,
    report_type,
    subject,
    title,
    filename,
    prompt,
    cron_expression,
    timezone,
    model,
    reasoning_effort,
    enabled,
    next_run_at,
    last_run_at
"""


def create_report_tasks_router() -> APIRouter:
    router = APIRouter(prefix="/api/report-tasks")

    @router.get("", response_model=list[ReportTask])
    async def list_tasks(request: Request) -> list[dict[str, object]]:
        database = _database(request)
        owner_user_id = _owner_user_id(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                f"""
                SELECT {_TASK_COLUMNS}
                FROM report_tasks
                WHERE owner_user_id = %s
                ORDER BY report_type, subject, created_at
                """,
                (owner_user_id,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @router.post("", response_model=ReportTask, status_code=201)
    async def create_task(
        payload: ReportTaskCreate,
        request: Request,
    ) -> dict[str, object]:
        report_type = _required(payload.report_type, "report_type")
        subject = _required(payload.subject, "subject")
        title = _required(payload.title, "title")
        filename = normalize_report_filename(payload.filename)
        prompt = _required(payload.prompt, "prompt")
        cron_expression = payload.cron_expression.strip()
        timezone = payload.timezone.strip()
        model = payload.model.strip()
        reasoning_effort = payload.reasoning_effort.strip()
        _validate_schedule(cron_expression, timezone)
        next_run_at = (
            _next_occurrence(cron_expression, timezone, datetime.now(UTC))
            if payload.enabled
            else None
        )

        database = _database(request)
        owner_user_id = _owner_user_id(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                f"""
                INSERT INTO report_tasks (
                    owner_user_id,
                    report_type,
                    subject,
                    title,
                    filename,
                    prompt,
                    cron_expression,
                    timezone,
                    model,
                    reasoning_effort,
                    enabled,
                    next_run_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_TASK_COLUMNS}
                """,
                (
                    owner_user_id,
                    report_type,
                    subject,
                    title,
                    filename,
                    prompt,
                    cron_expression,
                    timezone,
                    model,
                    reasoning_effort,
                    payload.enabled,
                    next_run_at,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise RuntimeError("report task insert returned no row")
        return dict(row)

    @router.patch("/{task_id}", response_model=ReportTask)
    async def update_task(
        task_id: UUID,
        payload: ReportTaskUpdate,
        request: Request,
    ) -> dict[str, object]:
        database = _database(request)
        owner_user_id = _owner_user_id(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    report_type,
                    subject,
                    title,
                    filename,
                    prompt,
                    cron_expression,
                    timezone,
                    model,
                    reasoning_effort,
                    enabled
                FROM report_tasks
                WHERE id = %s AND owner_user_id = %s
                FOR UPDATE
                """,
                (task_id, owner_user_id),
            )
            current = await cursor.fetchone()
            if current is None:
                raise HTTPException(status_code=404, detail="报告任务不存在")

            report_type = _required(
                payload.report_type or current["report_type"],
                "report_type",
            )
            subject = _required(payload.subject or current["subject"], "subject")
            title = _required(payload.title or current["title"], "title")
            filename = normalize_report_filename(
                payload.filename or current["filename"]
            )
            prompt = _required(payload.prompt or current["prompt"], "prompt")
            cron_expression = (
                payload.cron_expression or current["cron_expression"]
            ).strip()
            timezone = (payload.timezone or current["timezone"]).strip()
            model = (payload.model or current["model"]).strip()
            reasoning_effort = (
                payload.reasoning_effort or current["reasoning_effort"]
            ).strip()
            enabled = (
                payload.enabled
                if payload.enabled is not None
                else current["enabled"]
            )
            _validate_schedule(cron_expression, timezone)
            next_run_at = (
                _next_occurrence(cron_expression, timezone, datetime.now(UTC))
                if enabled
                else None
            )

            cursor = await connection.execute(
                f"""
                UPDATE report_tasks
                SET
                    report_type = %s,
                    subject = %s,
                    title = %s,
                    filename = %s,
                    prompt = %s,
                    cron_expression = %s,
                    timezone = %s,
                    model = %s,
                    reasoning_effort = %s,
                    enabled = %s,
                    next_run_at = %s,
                    updated_at = now()
                WHERE id = %s AND owner_user_id = %s
                RETURNING {_TASK_COLUMNS}
                """,
                (
                    report_type,
                    subject,
                    title,
                    filename,
                    prompt,
                    cron_expression,
                    timezone,
                    model,
                    reasoning_effort,
                    enabled,
                    next_run_at,
                    task_id,
                    owner_user_id,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise RuntimeError("report task update returned no row")
        return dict(row)

    return router


class ReportScheduler:
    def __init__(
        self,
        *,
        app: FastAPI,
        sandbox: SandboxProvider,
        database: Database,
    ) -> None:
        self._app = app
        self._sandbox = sandbox
        self._database = database
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._run_tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        await mark_interrupted_runs_failed(self._database)
        self._loop_task = asyncio.create_task(
            self._run_loop(),
            name="scheduled-investment-research-report-scheduler",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
        for task in tuple(self._run_tasks):
            task.cancel()
        if self._run_tasks:
            await asyncio.gather(*self._run_tasks, return_exceptions=True)

    async def _run_loop(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=3)
            return
        except TimeoutError:
            pass

        while not self._stop.is_set():
            try:
                runs = await _claim_due_runs(self._database)
                for run in runs:
                    task = asyncio.create_task(
                        self._execute(run),
                        name=f"report-run-{run.id}",
                    )
                    self._run_tasks.add(task)
                    task.add_done_callback(self._run_tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to claim scheduled report runs")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except TimeoutError:
                pass

    async def _execute(self, run: _ScheduledRun) -> None:
        try:
            await execute_report(
                app=self._app,
                sandbox=self._sandbox,
                database=self._database,
                run_id=run.id,
                owner_user_id=run.owner_user_id,
                report_type=run.report_type,
                subject=run.subject,
                title=run.title,
                filename=run.filename,
                prompt=run.prompt,
                model=run.model,
                reasoning_effort=run.reasoning_effort,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduled report run %s failed", run.id)


async def _claim_due_runs(database: Database) -> list[_ScheduledRun]:
    now = datetime.now(UTC)
    claimed: list[_ScheduledRun] = []
    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT
                id,
                owner_user_id,
                report_type,
                subject,
                title,
                filename,
                prompt,
                cron_expression,
                timezone,
                model,
                reasoning_effort,
                next_run_at
            FROM report_tasks
            WHERE enabled AND next_run_at <= %s
            ORDER BY next_run_at
            FOR UPDATE SKIP LOCKED
            LIMIT 10
            """,
            (now,),
        )
        tasks = await cursor.fetchall()
        for task in tasks:
            run_id = uuid4()
            scheduled_for = task["next_run_at"]
            cursor = await connection.execute(
                """
                INSERT INTO report_runs (
                    id,
                    task_id,
                    scheduled_for,
                    owner_user_id,
                    report_type,
                    subject,
                    title,
                    filename,
                    prompt,
                    status,
                    stage,
                    model,
                    reasoning_effort
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'queued', 'queued', %s, %s
                )
                ON CONFLICT (task_id, scheduled_for) DO NOTHING
                RETURNING id
                """,
                (
                    run_id,
                    task["id"],
                    scheduled_for,
                    task["owner_user_id"],
                    task["report_type"],
                    task["subject"],
                    task["title"],
                    task["filename"],
                    task["prompt"],
                    task["model"],
                    task["reasoning_effort"],
                ),
            )
            inserted = await cursor.fetchone()
            await connection.execute(
                """
                UPDATE report_tasks
                SET last_run_at = %s, next_run_at = %s, updated_at = now()
                WHERE id = %s
                """,
                (
                    scheduled_for,
                    _next_occurrence(
                        task["cron_expression"],
                        task["timezone"],
                        now,
                    ),
                    task["id"],
                ),
            )
            if inserted is not None:
                claimed.append(
                    _ScheduledRun(
                        id=run_id,
                        owner_user_id=task["owner_user_id"],
                        report_type=task["report_type"],
                        subject=task["subject"],
                        title=task["title"],
                        filename=task["filename"],
                        prompt=task["prompt"],
                        model=task["model"],
                        reasoning_effort=task["reasoning_effort"],
                    )
                )
        await connection.commit()
    if claimed:
        await notify_run_changed()
    return claimed


def _validate_schedule(cron_expression: str, timezone: str) -> None:
    if len(cron_expression.split()) != 5 or not croniter.is_valid(cron_expression):
        raise HTTPException(status_code=422, detail="cron 必须是合法的五段表达式")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise HTTPException(status_code=422, detail="时区不存在") from error


def _required(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(status_code=422, detail=f"{field} 不能为空")
    return normalized


def _next_occurrence(
    cron_expression: str,
    timezone: str,
    after: datetime,
) -> datetime:
    zone = ZoneInfo(timezone)
    local_after = after.astimezone(zone)
    next_local = croniter(cron_expression, local_after).get_next(datetime)
    return next_local.astimezone(UTC)


def _database(request: Request) -> Database:
    database = getattr(request.app.state, "report_database", None)
    if not isinstance(database, Database):
        raise RuntimeError("Scheduled Investment Research database is not ready")
    return database


def _owner_user_id(request: Request) -> str:
    owner_user_id = getattr(request.app.state, "report_owner_user_id", None)
    if not isinstance(owner_user_id, str) or not owner_user_id:
        raise RuntimeError("Scheduled Investment Research report owner is not ready")
    return owner_user_id
