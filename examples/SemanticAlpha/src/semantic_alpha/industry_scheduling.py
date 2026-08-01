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
from semantic_alpha.industry_reports import (
    REPORT_MODEL,
    REPORT_REASONING_EFFORT,
    execute_industry_report,
    mark_interrupted_runs_failed,
    notify_run_changed,
)


logger = logging.getLogger(__name__)


class IndustryScheduleCreate(BaseModel):
    industry: str = Field(min_length=1, max_length=80)
    cron_expression: str = Field(min_length=1, max_length=100)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    model: str = Field(default=REPORT_MODEL, min_length=1, max_length=120)
    reasoning_effort: str = Field(
        default=REPORT_REASONING_EFFORT,
        min_length=1,
        max_length=40,
    )
    enabled: bool = True


class IndustryScheduleUpdate(BaseModel):
    industry: str | None = Field(default=None, min_length=1, max_length=80)
    cron_expression: str | None = Field(default=None, min_length=1, max_length=100)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, min_length=1, max_length=120)
    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=40)
    enabled: bool | None = None


class IndustrySchedule(BaseModel):
    id: UUID
    created_at: datetime
    updated_at: datetime
    industry: str
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
    industry: str
    model: str
    reasoning_effort: str


def create_industry_schedules_router() -> APIRouter:
    router = APIRouter(prefix="/api/industry-report-schedules")

    @router.get("", response_model=list[IndustrySchedule])
    async def list_schedules(request: Request) -> list[dict[str, object]]:
        database = _database(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    id,
                    created_at,
                    updated_at,
                    industry,
                    cron_expression,
                    timezone,
                    model,
                    reasoning_effort,
                    enabled,
                    next_run_at,
                    last_run_at
                FROM industry_research_schedules
                ORDER BY created_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @router.post("", response_model=IndustrySchedule, status_code=201)
    async def create_schedule(
        payload: IndustryScheduleCreate,
        request: Request,
    ) -> dict[str, object]:
        industry = payload.industry.strip()
        cron_expression = payload.cron_expression.strip()
        timezone = payload.timezone.strip()
        _validate_schedule(cron_expression, timezone)
        next_run_at = (
            _next_occurrence(cron_expression, timezone, datetime.now(UTC))
            if payload.enabled
            else None
        )

        database = _database(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO industry_research_schedules (
                    industry,
                    cron_expression,
                    timezone,
                    model,
                    reasoning_effort,
                    enabled,
                    next_run_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING
                    id,
                    created_at,
                    updated_at,
                    industry,
                    cron_expression,
                    timezone,
                    model,
                    reasoning_effort,
                    enabled,
                    next_run_at,
                    last_run_at
                """,
                (
                    industry,
                    cron_expression,
                    timezone,
                    payload.model.strip(),
                    payload.reasoning_effort.strip(),
                    payload.enabled,
                    next_run_at,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise RuntimeError("industry schedule insert returned no row")
        return dict(row)

    @router.patch("/{schedule_id}", response_model=IndustrySchedule)
    async def update_schedule(
        schedule_id: UUID,
        payload: IndustryScheduleUpdate,
        request: Request,
    ) -> dict[str, object]:
        database = _database(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    industry,
                    cron_expression,
                    timezone,
                    model,
                    reasoning_effort,
                    enabled
                FROM industry_research_schedules
                WHERE id = %s
                FOR UPDATE
                """,
                (schedule_id,),
            )
            current = await cursor.fetchone()
            if current is None:
                raise HTTPException(status_code=404, detail="定时任务不存在")

            industry = (payload.industry or current["industry"]).strip()
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
                """
                UPDATE industry_research_schedules
                SET
                    industry = %s,
                    cron_expression = %s,
                    timezone = %s,
                    model = %s,
                    reasoning_effort = %s,
                    enabled = %s,
                    next_run_at = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING
                    id,
                    created_at,
                    updated_at,
                    industry,
                    cron_expression,
                    timezone,
                    model,
                    reasoning_effort,
                    enabled,
                    next_run_at,
                    last_run_at
                """,
                (
                    industry,
                    cron_expression,
                    timezone,
                    model,
                    reasoning_effort,
                    enabled,
                    next_run_at,
                    schedule_id,
                ),
            )
            row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise RuntimeError("industry schedule update returned no row")
        return dict(row)

    return router


class IndustryResearchScheduler:
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
            name="semantic-alpha-industry-scheduler",
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
                        name=f"industry-research-run-{run.id}",
                    )
                    self._run_tasks.add(task)
                    task.add_done_callback(self._run_tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("failed to claim scheduled industry research runs")

            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except TimeoutError:
                pass

    async def _execute(self, run: _ScheduledRun) -> None:
        try:
            await execute_industry_report(
                app=self._app,
                sandbox=self._sandbox,
                database=self._database,
                run_id=run.id,
                industry=run.industry,
                model=run.model,
                reasoning_effort=run.reasoning_effort,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduled industry research run %s failed", run.id)


async def _claim_due_runs(database: Database) -> list[_ScheduledRun]:
    now = datetime.now(UTC)
    claimed: list[_ScheduledRun] = []
    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            SELECT
                id,
                industry,
                cron_expression,
                timezone,
                model,
                reasoning_effort,
                next_run_at
            FROM industry_research_schedules
            WHERE enabled AND next_run_at <= %s
            ORDER BY next_run_at
            FOR UPDATE SKIP LOCKED
            LIMIT 10
            """,
            (now,),
        )
        schedules = await cursor.fetchall()
        for schedule in schedules:
            run_id = uuid4()
            scheduled_for = schedule["next_run_at"]
            cursor = await connection.execute(
                """
                INSERT INTO industry_research_runs (
                    id,
                    schedule_id,
                    scheduled_for,
                    trigger,
                    industry,
                    status,
                    stage,
                    model,
                    reasoning_effort
                )
                VALUES (%s, %s, %s, 'scheduled', %s, 'queued', 'queued', %s, %s)
                ON CONFLICT (schedule_id, scheduled_for) DO NOTHING
                RETURNING id
                """,
                (
                    run_id,
                    schedule["id"],
                    scheduled_for,
                    schedule["industry"],
                    schedule["model"],
                    schedule["reasoning_effort"],
                ),
            )
            inserted = await cursor.fetchone()
            await connection.execute(
                """
                UPDATE industry_research_schedules
                SET last_run_at = %s, next_run_at = %s, updated_at = now()
                WHERE id = %s
                """,
                (
                    scheduled_for,
                    _next_occurrence(
                        schedule["cron_expression"],
                        schedule["timezone"],
                        now,
                    ),
                    schedule["id"],
                ),
            )
            if inserted is not None:
                claimed.append(
                    _ScheduledRun(
                        id=run_id,
                        industry=schedule["industry"],
                        model=schedule["model"],
                        reasoning_effort=schedule["reasoning_effort"],
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
    database = getattr(request.app.state, "industry_report_database", None)
    if not isinstance(database, Database):
        raise RuntimeError("Semantic Alpha database is not ready")
    return database
