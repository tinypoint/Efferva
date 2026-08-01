from __future__ import annotations

import asyncio
import json
import posixpath
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from efferva import SandboxContext, SandboxProvider
from efferva.db import Database


REPORT_MODEL = "gpt-5.6-sol"
REPORT_REASONING_EFFORT = "ultra"


class IndustryReportCreate(BaseModel):
    industry: str = Field(min_length=1, max_length=80)


class IndustryReportCreated(BaseModel):
    id: UUID
    run_id: UUID
    created_at: datetime
    session_id: UUID
    thread_id: str
    model: str
    reasoning_effort: str
    markdown_path: str


class IndustryReportSummary(BaseModel):
    id: UUID
    created_at: datetime
    title: str


class IndustryReport(BaseModel):
    id: UUID
    created_at: datetime
    session_id: UUID | None
    thread_id: str | None
    model: str | None
    reasoning_effort: str | None
    markdown: str


class IndustryReportRun(BaseModel):
    id: UUID
    created_at: datetime
    scheduled_for: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    schedule_id: UUID | None
    trigger: str
    industry: str
    status: str
    stage: str
    session_id: UUID | None
    thread_id: str | None
    model: str
    reasoning_effort: str
    report_id: UUID | None
    error: str | None


class _RunChangeSignal:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    async def notify(self) -> None:
        async with self._condition:
            self._revision += 1
            self._condition.notify_all()

    async def wait(self, revision: int) -> int:
        async with self._condition:
            if self._revision == revision:
                try:
                    async with asyncio.timeout(15):
                        await self._condition.wait_for(
                            lambda: self._revision != revision
                        )
                except TimeoutError:
                    pass
            return self._revision


_run_changes = _RunChangeSignal()


def create_industry_reports_router(
    sandbox: SandboxProvider,
) -> APIRouter:
    router = APIRouter()
    reports = APIRouter(prefix="/api/industry-reports")
    runs = APIRouter(prefix="/api/industry-report-runs")

    @reports.get("", response_model=list[IndustryReportSummary])
    async def list_industry_reports(request: Request) -> list[dict[str, object]]:
        database = _database(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT id, created_at, markdown
                FROM industry_research_reports
                ORDER BY created_at DESC
                """
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "title": _markdown_title(row["markdown"]),
            }
            for row in rows
        ]

    @reports.post("", response_model=IndustryReportCreated, status_code=201)
    async def generate_industry_report(
        payload: IndustryReportCreate,
        request: Request,
    ) -> dict[str, object]:
        industry = payload.industry.strip()
        if not industry:
            raise HTTPException(status_code=422, detail="industry must not be blank")

        database = _database(request)
        run_id = await create_report_run(
            database,
            industry=industry,
            trigger="manual",
            model=REPORT_MODEL,
            reasoning_effort=REPORT_REASONING_EFFORT,
        )
        return await execute_industry_report(
            app=request.app,
            sandbox=sandbox,
            database=database,
            run_id=run_id,
            industry=industry,
            model=REPORT_MODEL,
            reasoning_effort=REPORT_REASONING_EFFORT,
        )

    @reports.get("/{report_id}", response_model=IndustryReport)
    async def get_industry_report(
        report_id: UUID,
        request: Request,
    ) -> dict[str, object]:
        database = _database(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    id,
                    created_at,
                    session_id,
                    thread_id,
                    model,
                    reasoning_effort,
                    markdown
                FROM industry_research_reports
                WHERE id = %s
                """,
                (report_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="产业调查报告不存在")
        return dict(row)

    @runs.get("", response_model=list[IndustryReportRun])
    async def list_industry_report_runs(
        request: Request,
        limit: int = Query(default=100, ge=1, le=200),
    ) -> list[dict[str, object]]:
        database = _database(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    id,
                    created_at,
                    scheduled_for,
                    started_at,
                    finished_at,
                    schedule_id,
                    trigger,
                    industry,
                    status,
                    stage,
                    session_id,
                    thread_id,
                    model,
                    reasoning_effort,
                    report_id,
                    error
                FROM industry_research_runs
                ORDER BY
                    CASE WHEN status IN ('queued', 'running') THEN 0 ELSE 1 END,
                    created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @runs.get("/events")
    async def stream_industry_report_run_events(
        request: Request,
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            revision = _run_changes.revision
            yield f"event: changed\ndata: {revision}\n\n"
            while not await request.is_disconnected():
                revision = await _run_changes.wait(revision)
                yield f"event: changed\ndata: {revision}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    router.include_router(reports)
    router.include_router(runs)
    return router


async def create_report_run(
    database: Database,
    *,
    industry: str,
    trigger: str,
    model: str,
    reasoning_effort: str,
    schedule_id: UUID | None = None,
    scheduled_for: datetime | None = None,
) -> UUID:
    run_id = uuid4()
    async with database.connection() as connection:
        await connection.execute(
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
            VALUES (%s, %s, %s, %s, %s, 'queued', 'queued', %s, %s)
            """,
            (
                run_id,
                schedule_id,
                scheduled_for,
                trigger,
                industry,
                model,
                reasoning_effort,
            ),
        )
        await connection.commit()
    await notify_run_changed()
    return run_id


async def execute_industry_report(
    *,
    app: FastAPI,
    sandbox: SandboxProvider,
    database: Database,
    run_id: UUID,
    industry: str,
    model: str,
    reasoning_effort: str,
) -> dict[str, object]:
    markdown_path = posixpath.join(
        "/home/sandbox/workspace/reports",
        f"industry-research-{run_id.hex}.md",
    )
    prompt = _research_prompt(industry, markdown_path)

    try:
        await _mark_run_started(database, run_id)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://semantic-alpha",
            timeout=None,
        ) as client:
            session_response = await client.post(
                "/agent/api/sessions",
                json={"name": f"产业调查 · {industry}"},
            )
            _raise_for_efferva(session_response, "创建 Session")
            session_id = UUID(session_response.json()["id"])
            await _mark_run_session_created(database, run_id, session_id)

            async def on_thread_created(thread_id: str) -> None:
                await _mark_run_thread_created(database, run_id, thread_id)

            async with client.stream(
                "POST",
                "/agent/api/ag-ui",
                json={
                    "threadId": "new",
                    "runId": str(run_id),
                    "messages": [
                        {
                            "id": f"industry-report-{run_id}",
                            "role": "user",
                            "content": prompt,
                        }
                    ],
                    "forwardedProps": {
                        "sessionId": str(session_id),
                        "workspace": "/home/sandbox/workspace",
                        "model": model,
                        "reasoningEffort": reasoning_effort,
                    },
                },
            ) as turn_response:
                _raise_for_efferva(turn_response, "生成产业调查报告")
                thread_id = await _wait_for_turn(
                    turn_response,
                    on_thread_created=on_thread_created,
                )

        await _set_run_stage(database, run_id, "reading_report")
        context = SandboxContext(
            session_id=session_id,
            workspace_path="/home/sandbox/workspace",
        )
        volume = await sandbox.ensure_session_volume(context)
        handle = await sandbox.start(context, volume)
        runtime = await sandbox.connect(handle)
        try:
            await runtime.stat(markdown_path)
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=502,
                detail=f"Agent 未生成 Markdown 文件：{markdown_path}",
            ) from error
        markdown = (await runtime.read_file(markdown_path)).decode("utf-8").strip()
        if not markdown:
            raise HTTPException(status_code=502, detail="Agent 生成了空报告")

        await _set_run_stage(database, run_id, "saving_report")
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO industry_research_reports (
                    session_id,
                    thread_id,
                    model,
                    reasoning_effort,
                    markdown
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (session_id, thread_id, model, reasoning_effort, markdown),
            )
            saved = await cursor.fetchone()
            if saved is None:
                raise RuntimeError("industry report insert returned no row")
            await connection.execute(
                """
                UPDATE industry_research_runs
                SET
                    status = 'succeeded',
                    stage = 'completed',
                    finished_at = now(),
                    report_id = %s,
                    error = NULL
                WHERE id = %s
                """,
                (saved["id"], run_id),
            )
            await connection.commit()
        await notify_run_changed()

        return {
            "id": saved["id"],
            "run_id": run_id,
            "created_at": saved["created_at"],
            "session_id": session_id,
            "thread_id": thread_id,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "markdown_path": markdown_path,
        }
    except asyncio.CancelledError:
        await _mark_run_failed(database, run_id, "服务停止，执行被中断")
        raise
    except Exception as error:
        await _mark_run_failed(database, run_id, _error_message(error))
        raise


async def notify_run_changed() -> None:
    await _run_changes.notify()


async def mark_interrupted_runs_failed(database: Database) -> None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            UPDATE industry_research_runs
            SET
                status = 'failed',
                stage = 'interrupted',
                finished_at = now(),
                error = 'Semantic Alpha 服务重启，执行被中断'
            WHERE status IN ('queued', 'running')
            """
        )
        await connection.commit()
    if cursor.rowcount:
        await notify_run_changed()


async def _mark_run_started(database: Database, run_id: UUID) -> None:
    async with database.connection() as connection:
        await connection.execute(
            """
            UPDATE industry_research_runs
            SET status = 'running', stage = 'creating_session', started_at = now()
            WHERE id = %s
            """,
            (run_id,),
        )
        await connection.commit()
    await notify_run_changed()


async def _mark_run_session_created(
    database: Database,
    run_id: UUID,
    session_id: UUID,
) -> None:
    async with database.connection() as connection:
        await connection.execute(
            """
            UPDATE industry_research_runs
            SET session_id = %s, stage = 'creating_thread'
            WHERE id = %s
            """,
            (session_id, run_id),
        )
        await connection.commit()
    await notify_run_changed()


async def _mark_run_thread_created(
    database: Database,
    run_id: UUID,
    thread_id: str,
) -> None:
    async with database.connection() as connection:
        await connection.execute(
            """
            UPDATE industry_research_runs
            SET thread_id = %s, stage = 'researching'
            WHERE id = %s
            """,
            (thread_id, run_id),
        )
        await connection.commit()
    await notify_run_changed()


async def _set_run_stage(database: Database, run_id: UUID, stage: str) -> None:
    async with database.connection() as connection:
        await connection.execute(
            """
            UPDATE industry_research_runs
            SET stage = %s
            WHERE id = %s
            """,
            (stage, run_id),
        )
        await connection.commit()
    await notify_run_changed()


async def _mark_run_failed(
    database: Database,
    run_id: UUID,
    error: str,
) -> None:
    async with database.connection() as connection:
        await connection.execute(
            """
            UPDATE industry_research_runs
            SET
                status = 'failed',
                stage = 'failed',
                finished_at = now(),
                error = %s
            WHERE id = %s AND status <> 'succeeded'
            """,
            (error[:4000], run_id),
        )
        await connection.commit()
    await notify_run_changed()


def _database(request: Request) -> Database:
    database = getattr(request.app.state, "industry_report_database", None)
    if not isinstance(database, Database):
        raise RuntimeError("Semantic Alpha database is not ready")
    return database


def _raise_for_efferva(response: httpx.Response, action: str) -> None:
    if not response.is_error:
        return
    try:
        detail = response.json().get("detail")
    except (json.JSONDecodeError, AttributeError):
        detail = response.text
    raise HTTPException(
        status_code=502,
        detail=f"{action}失败：{detail or response.status_code}",
    )


async def _wait_for_turn(
    response: httpx.Response,
    *,
    on_thread_created: Callable[[str], Awaitable[None]],
) -> str:
    finished = False
    thread_id: str | None = None
    async for line in response.aiter_lines():
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line.removeprefix("data: "))
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        discovered_thread_id: str | None = None
        if event_type == "RUN_STARTED" and event.get("threadId") not in {None, "new"}:
            discovered_thread_id = str(event["threadId"])
        if event_type == "RAW":
            raw = event.get("event") or {}
            if raw.get("method") == "efferva/thread-created":
                thread = (raw.get("params") or {}).get("thread") or {}
                if thread.get("id"):
                    discovered_thread_id = str(thread["id"])
        if discovered_thread_id and discovered_thread_id != thread_id:
            thread_id = discovered_thread_id
            await on_thread_created(thread_id)
        if event_type == "RUN_ERROR":
            raise HTTPException(
                status_code=502,
                detail=f"Agent 生成失败：{event.get('message') or 'unknown error'}",
            )
        if event_type == "RUN_FINISHED":
            finished = True
    if not finished:
        raise HTTPException(status_code=502, detail="Agent 生成流意外结束")
    if thread_id is None:
        raise HTTPException(status_code=502, detail="Agent 未返回新建 Thread")
    return thread_id


def _error_message(error: Exception) -> str:
    if isinstance(error, HTTPException):
        detail = error.detail
        if isinstance(detail, str):
            return detail
        return json.dumps(detail, ensure_ascii=False)
    return str(error) or error.__class__.__name__


def _research_prompt(industry: str, markdown_path: str) -> str:
    return f"""$industry-research {industry}

严格使用 Industry Research skill 完成一份中文产业链投资研究报告。

执行约束：
1. 可按研究需要创建和调用 subagent；最终由当前 Agent 汇总、核验并交付报告。
2. 先运行 date，以当天为数据截止日；需要最新信息时必须联网核实并附来源。
3. 遵循 skill 的产业链、全球公司扫描、四大师框架、风险、组合建议和数据抽检要求。
4. 不得伪造数据；无法双源验证的内容明确标为低置信度或待核实。
5. 将最终完整 Markdown 写入这个绝对路径：{markdown_path}
6. 文件写入成功后再结束，最终回复只报告文件路径与抽检结论。
"""


def _markdown_title(markdown: str) -> str:
    for line in markdown.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    return "产业调查报告"
