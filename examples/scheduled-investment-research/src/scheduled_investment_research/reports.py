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
from pydantic import BaseModel

from efferva import SandboxContext, SandboxProvider
from efferva.db import Database


REPORT_MODEL = "gpt-5.6-sol"
REPORT_REASONING_EFFORT = "ultra"
_WORKSPACE = "/home/sandbox/workspace"


class ReportRun(BaseModel):
    id: UUID
    created_at: datetime
    scheduled_for: datetime
    started_at: datetime | None
    finished_at: datetime | None
    task_id: UUID
    owner_user_id: str
    report_type: str
    subject: str
    title: str
    filename: str
    status: str
    stage: str
    session_id: UUID | None
    thread_id: str | None
    model: str
    reasoning_effort: str
    duration_seconds: float | None
    report_id: UUID | None
    error: str | None


class ReportRunDetail(ReportRun):
    markdown: str | None


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


def create_report_runs_router() -> APIRouter:
    router = APIRouter(prefix="/api/report-runs")

    @router.get("", response_model=list[ReportRun])
    async def list_report_runs(
        request: Request,
        limit: int = Query(default=500, ge=1, le=1000),
    ) -> list[dict[str, object]]:
        database = _database(request)
        owner_user_id = _owner_user_id(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    id,
                    created_at,
                    scheduled_for,
                    started_at,
                    finished_at,
                    task_id,
                    owner_user_id,
                    report_type,
                    subject,
                    title,
                    filename,
                    status,
                    stage,
                    session_id,
                    thread_id,
                    model,
                    reasoning_effort,
                    EXTRACT(
                        EPOCH FROM (COALESCE(finished_at, now()) - started_at)
                    )::double precision AS duration_seconds,
                    report_id,
                    error
                FROM report_runs
                WHERE owner_user_id = %s
                ORDER BY scheduled_for DESC, created_at DESC
                LIMIT %s
                """,
                (owner_user_id, limit),
            )
            rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    @router.get("/events")
    async def stream_report_run_events(request: Request) -> StreamingResponse:
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

    @router.get("/{run_id}", response_model=ReportRunDetail)
    async def get_report_run(
        run_id: UUID,
        request: Request,
    ) -> dict[str, object]:
        database = _database(request)
        owner_user_id = _owner_user_id(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT
                    run.id,
                    run.created_at,
                    run.scheduled_for,
                    run.started_at,
                    run.finished_at,
                    run.task_id,
                    run.owner_user_id,
                    run.report_type,
                    run.subject,
                    run.title,
                    run.filename,
                    run.status,
                    run.stage,
                    run.session_id,
                    run.thread_id,
                    run.model,
                    run.reasoning_effort,
                    EXTRACT(
                        EPOCH FROM (
                            COALESCE(run.finished_at, now()) - run.started_at
                        )
                    )::double precision AS duration_seconds,
                    run.report_id,
                    run.error,
                    report.markdown
                FROM report_runs AS run
                LEFT JOIN reports AS report ON report.id = run.report_id
                WHERE run.id = %s AND run.owner_user_id = %s
                """,
                (run_id, owner_user_id),
            )
            row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="报告运行不存在")
        return dict(row)

    return router


async def execute_report(
    *,
    app: FastAPI,
    sandbox: SandboxProvider,
    database: Database,
    run_id: UUID,
    owner_user_id: str,
    report_type: str,
    subject: str,
    title: str,
    filename: str,
    prompt: str,
    model: str,
    reasoning_effort: str,
) -> dict[str, object]:
    markdown_path = _markdown_path(filename)
    execution_prompt = _execution_prompt(prompt, markdown_path)

    try:
        await _mark_run_started(database, run_id)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://scheduled-investment-research",
            timeout=None,
        ) as client:
            session_response = await client.post(
                "/agent/api/sessions",
                json={"name": title},
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
                            "id": f"report-{run_id}",
                            "role": "user",
                            "content": execution_prompt,
                        }
                    ],
                    "forwardedProps": {
                        "sessionId": str(session_id),
                        "workspace": _WORKSPACE,
                        "model": model,
                        "reasoningEffort": reasoning_effort,
                    },
                },
            ) as turn_response:
                _raise_for_efferva(turn_response, "生成报告")
                thread_id = await _wait_for_turn(
                    turn_response,
                    on_thread_created=on_thread_created,
                )

        await _set_run_stage(database, run_id, "reading_report")
        context = SandboxContext(
            session_id=session_id,
            tenant_id="local",
            owner_issuer="scheduled-investment-research",
            owner_subject="developer",
            workspace_path=_WORKSPACE,
        )
        environment = await sandbox.ensure(context)
        runtime = environment.runtime
        try:
            await runtime.stat(markdown_path)
        except FileNotFoundError:
            await _set_run_stage(database, run_id, "finalizing_report")
            await _finalize_missing_report(
                app=app,
                session_id=session_id,
                thread_id=thread_id,
                report_type=report_type,
                subject=subject,
                title=title,
                markdown_path=markdown_path,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            await _set_run_stage(database, run_id, "reading_report")
            try:
                await runtime.stat(markdown_path)
            except FileNotFoundError as final_error:
                raise HTTPException(
                    status_code=502,
                    detail=f"Agent 两轮均未生成 Markdown 文件：{markdown_path}",
                ) from final_error

        markdown = (await runtime.read_file(markdown_path)).decode("utf-8").strip()
        if not markdown:
            raise HTTPException(status_code=502, detail="Agent 生成了空报告")

        await _set_run_stage(database, run_id, "saving_report")
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO reports (
                    owner_user_id,
                    report_type,
                    subject,
                    title,
                    filename,
                    session_id,
                    thread_id,
                    model,
                    reasoning_effort,
                    markdown
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (
                    owner_user_id,
                    report_type,
                    subject,
                    title,
                    filename,
                    session_id,
                    thread_id,
                    model,
                    reasoning_effort,
                    markdown,
                ),
            )
            saved = await cursor.fetchone()
            if saved is None:
                raise RuntimeError("report insert returned no row")
            await connection.execute(
                """
                UPDATE report_runs
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
            "report_type": report_type,
            "subject": subject,
            "title": title,
            "filename": filename,
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


def normalize_report_filename(filename: str) -> str:
    normalized = filename.strip()
    if (
        not normalized
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
        or not normalized.lower().endswith(".md")
        or normalized.lower() == ".md"
    ):
        raise HTTPException(
            status_code=422,
            detail="filename 必须是不含路径的 Markdown 文件名",
        )
    return normalized


async def notify_run_changed() -> None:
    await _run_changes.notify()


async def mark_interrupted_runs_failed(database: Database) -> None:
    async with database.connection() as connection:
        cursor = await connection.execute(
            """
            UPDATE report_runs
            SET
                status = 'failed',
                stage = 'interrupted',
                finished_at = now(),
                error = 'Scheduled Investment Research 服务重启，执行被中断'
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
            UPDATE report_runs
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
            UPDATE report_runs
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
            UPDATE report_runs
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
            UPDATE report_runs
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
            UPDATE report_runs
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
    database = getattr(request.app.state, "report_database", None)
    if not isinstance(database, Database):
        raise RuntimeError("Scheduled Investment Research database is not ready")
    return database


def _owner_user_id(request: Request) -> str:
    owner_user_id = getattr(request.app.state, "report_owner_user_id", None)
    if not isinstance(owner_user_id, str) or not owner_user_id:
        raise RuntimeError("Scheduled Investment Research report owner is not ready")
    return owner_user_id


def _markdown_path(filename: str) -> str:
    return posixpath.join(_WORKSPACE, normalize_report_filename(filename))


def _execution_prompt(prompt: str, markdown_path: str) -> str:
    return f"""{prompt.strip()}

交付约束：
1. 最终交付物必须是一份完整 Markdown 文件。
2. 将最终 Markdown 写入绝对路径：{markdown_path}
3. 写入后确认文件存在且非空；最终回复只报告文件路径与字节数。
"""


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


async def _finalize_missing_report(
    *,
    app: FastAPI,
    session_id: UUID,
    thread_id: str,
    report_type: str,
    subject: str,
    title: str,
    markdown_path: str,
    model: str,
    reasoning_effort: str,
) -> None:
    followup_run_id = uuid4()

    async def confirm_thread(discovered_thread_id: str) -> None:
        if discovered_thread_id != thread_id:
            raise HTTPException(status_code=502, detail="Agent 收尾时切换了 Thread")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://scheduled-investment-research",
        timeout=None,
    ) as client:
        async with client.stream(
            "POST",
            "/agent/api/ag-ui",
            json={
                "threadId": thread_id,
                "runId": str(followup_run_id),
                "messages": [
                    {
                        "id": f"report-finalize-{followup_run_id}",
                        "role": "user",
                        "content": _finalize_prompt(
                            report_type,
                            subject,
                            title,
                            markdown_path,
                        ),
                    }
                ],
                "forwardedProps": {
                    "sessionId": str(session_id),
                    "workspace": _WORKSPACE,
                    "model": model,
                    "reasoningEffort": reasoning_effort,
                },
            },
        ) as response:
            _raise_for_efferva(response, "收尾报告")
            await _wait_for_turn(response, on_thread_created=confirm_thread)


def _finalize_prompt(
    report_type: str,
    subject: str,
    title: str,
    markdown_path: str,
) -> str:
    return f"""上一轮任务已经结束，但最终 Markdown 没有按约定路径落盘。

不要重新研究，不要创建新 Thread 或 subagent。把本线程已经完成的报告内容整理为完整 Markdown 并写入：{markdown_path}
报告类型：{report_type}
报告主题：{subject}
报告标题：{title}
写入后确认文件存在且非空；最终回复只报告文件路径、字节数与自检结果。
"""
