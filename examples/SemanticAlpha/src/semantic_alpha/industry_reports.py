from __future__ import annotations

import json
import posixpath
from datetime import datetime
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from efferva import SandboxContext, SandboxProvider
from efferva.db import Database


class IndustryReportCreate(BaseModel):
    industry: str = Field(min_length=1, max_length=80)


class IndustryReportCreated(BaseModel):
    id: UUID
    created_at: datetime
    session_id: UUID
    thread_id: str
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
    markdown: str


def create_industry_reports_router(
    sandbox: SandboxProvider,
) -> APIRouter:
    router = APIRouter(prefix="/api/industry-reports")

    @router.get("", response_model=list[IndustryReportSummary])
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

    @router.post("", response_model=IndustryReportCreated, status_code=201)
    async def generate_industry_report(
        payload: IndustryReportCreate,
        request: Request,
    ) -> dict[str, object]:
        industry = payload.industry.strip()
        if not industry:
            raise HTTPException(status_code=422, detail="industry must not be blank")

        report_token = uuid4()
        markdown_path = posixpath.join(
            "/home/sandbox/workspace/reports",
            f"industry-research-{report_token.hex}.md",
        )
        prompt = _research_prompt(industry, markdown_path)

        transport = httpx.ASGITransport(
            app=request.app,
            raise_app_exceptions=False,
        )
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

            async with client.stream(
                "POST",
                "/agent/api/ag-ui",
                json={
                    "threadId": "new",
                    "runId": str(report_token),
                    "messages": [
                        {
                            "id": f"industry-report-{report_token}",
                            "role": "user",
                            "content": prompt,
                        }
                    ],
                    "forwardedProps": {
                        "sessionId": str(session_id),
                        "workspace": "/home/sandbox/workspace",
                    },
                },
            ) as turn_response:
                _raise_for_efferva(turn_response, "生成产业调查报告")
                thread_id = await _wait_for_turn(turn_response)

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

        database = _database(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO industry_research_reports (
                    session_id,
                    thread_id,
                    markdown
                )
                VALUES (%s, %s, %s)
                RETURNING id, created_at
                """,
                (session_id, thread_id, markdown),
            )
            saved = await cursor.fetchone()
            await connection.commit()
        if saved is None:
            raise RuntimeError("industry report insert returned no row")

        return {
            "id": saved["id"],
            "created_at": saved["created_at"],
            "session_id": session_id,
            "thread_id": thread_id,
            "markdown_path": markdown_path,
        }

    @router.get("/{report_id}", response_model=IndustryReport)
    async def get_industry_report(
        report_id: UUID,
        request: Request,
    ) -> dict[str, object]:
        database = _database(request)
        async with database.connection() as connection:
            cursor = await connection.execute(
                """
                SELECT id, created_at, session_id, thread_id, markdown
                FROM industry_research_reports
                WHERE id = %s
                """,
                (report_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="产业调查报告不存在")
        return dict(row)

    return router


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


async def _wait_for_turn(response: httpx.Response) -> str:
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
        if event_type == "RAW":
            raw = event.get("event") or {}
            if raw.get("method") == "efferva/thread-created":
                thread = (raw.get("params") or {}).get("thread") or {}
                if thread.get("id"):
                    thread_id = str(thread["id"])
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


def _research_prompt(industry: str, markdown_path: str) -> str:
    return f"""$industry-research {industry}

严格使用 Industry Research skill 完成一份中文产业链投资研究报告。

执行约束：
1. 禁止创建或调用任何 subagent，所有研究在当前 Agent 内串行完成。
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
