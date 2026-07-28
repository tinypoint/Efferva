import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from efferva.identity import IdentityResolver, Principal
from efferva.models import (
    PrincipalView,
    Run,
    RunAgentInput,
    RunCreate,
    Session,
    SessionCreate,
    Thread,
    ThreadCreate,
    ThreadDetail,
)
from efferva.repository import AuthorizedRepository, NotFoundError, SystemRepository


def principal_dependency(
    identity: IdentityResolver,
) -> Callable[[Request], Any]:
    async def resolve(request: Request) -> Principal:
        principal = await identity(request)
        if not isinstance(principal, Principal):
            raise TypeError("IdentityResolver must return efferva.Principal")
        return principal

    return resolve


def _prompt_from_agui(input_payload: RunAgentInput) -> str:
    for message in reversed(input_payload.messages):
        if message.role != "user":
            continue
        if isinstance(message.content, str):
            return message.content
        if isinstance(message.content, list):
            text_parts = [
                part.get("text", "")
                for part in message.content
                if part.get("type") in {"text", "input_text"}
            ]
            prompt = "".join(text_parts)
            if prompt:
                return prompt
    raise HTTPException(status_code=422, detail="AG-UI input requires a user message")


def _sse(seq: int, event: dict[str, Any]) -> str:
    return f"id: {seq}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


def _stream_cursor(request: Request, query_cursor: int) -> int:
    header = request.headers.get("last-event-id", "")
    header_cursor = int(header) if header.isdigit() else 0
    return max(query_cursor, header_cursor)


async def _stream_run(
    request: Request,
    repository: AuthorizedRepository,
    run_id: UUID,
    after: int,
) -> AsyncIterator[str]:
    cursor = after
    while True:
        events = await repository.list_run_events(run_id, cursor)
        if events:
            for stored in events:
                cursor = stored["seq"]
                yield _sse(cursor, stored["event"])
                if stored["event"]["type"] in {"RUN_FINISHED", "RUN_ERROR"}:
                    return
            continue
        if await request.is_disconnected():
            return
        if await repository.run_is_terminal(run_id):
            return
        yield ": keep-alive\n\n"
        await asyncio.sleep(1)


def create_api_router(
    *,
    identity: IdentityResolver,
    system_repository: Callable[[], SystemRepository],
    worker_healthy: Callable[[], bool],
) -> APIRouter:
    router = APIRouter()
    resolve_principal = principal_dependency(identity)
    PrincipalParameter = Annotated[Principal, Depends(resolve_principal)]

    def authorized(principal: Principal) -> AuthorizedRepository:
        return system_repository().for_principal(principal)

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        repository = system_repository()
        await repository.ping()
        if not worker_healthy():
            raise HTTPException(status_code=503, detail="run worker is not healthy")
        return {"status": "ok"}

    @router.get("/api/meta", include_in_schema=False)
    async def metadata(request: Request, _: PrincipalParameter) -> dict[str, str]:
        return {"title": request.app.title}

    @router.get("/api/me", response_model=PrincipalView)
    async def me(principal: PrincipalParameter) -> dict[str, Any]:
        return {
            "tenant_id": principal.tenant_id,
            "issuer": principal.issuer,
            "subject": principal.subject,
            "capabilities": sorted(principal.capabilities, key=lambda item: item.value),
        }

    @router.post("/api/sessions", response_model=Session, status_code=201)
    async def create_session(
        payload: SessionCreate,
        principal: PrincipalParameter,
    ) -> dict[str, Any]:
        return await authorized(principal).create_session(payload.name)

    @router.get("/api/sessions", response_model=list[Session])
    async def list_sessions(
        principal: PrincipalParameter,
        scope: Literal["mine", "tenant"] = Query(default="mine"),
    ) -> list[dict[str, Any]]:
        return await authorized(principal).list_sessions(scope)

    @router.get("/api/sessions/{session_id}", response_model=Session)
    async def get_session(
        session_id: UUID,
        principal: PrincipalParameter,
    ) -> dict[str, Any]:
        return await authorized(principal).get_session(session_id)

    @router.post(
        "/api/sessions/{session_id}/threads",
        response_model=Thread,
        status_code=201,
    )
    async def create_thread(
        session_id: UUID,
        payload: ThreadCreate,
        principal: PrincipalParameter,
    ) -> dict[str, Any]:
        return await authorized(principal).create_thread(session_id, payload.title)

    @router.get("/api/sessions/{session_id}/threads", response_model=list[Thread])
    async def list_threads(
        session_id: UUID,
        principal: PrincipalParameter,
    ) -> list[dict[str, Any]]:
        return await authorized(principal).list_threads(session_id)

    @router.get("/api/threads/{thread_id}", response_model=ThreadDetail)
    async def get_thread(
        thread_id: UUID,
        principal: PrincipalParameter,
    ) -> dict[str, Any]:
        return await authorized(principal).get_thread_detail(thread_id)

    @router.post("/api/threads/{thread_id}/runs", response_model=Run, status_code=202)
    async def create_run(
        thread_id: UUID,
        payload: RunCreate,
        principal: PrincipalParameter,
    ) -> dict[str, Any]:
        return await authorized(principal).create_run(thread_id, payload.prompt)

    @router.get("/api/runs/{run_id}", response_model=Run)
    async def get_run(run_id: UUID, principal: PrincipalParameter) -> dict[str, Any]:
        return await authorized(principal).get_run(run_id)

    @router.get("/api/runs/{run_id}/events")
    async def list_run_events(
        run_id: UUID,
        principal: PrincipalParameter,
        after: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return await authorized(principal).list_run_events(run_id, after)

    @router.get("/api/runs/{run_id}/events/stream")
    async def stream_run_events(
        run_id: UUID,
        request: Request,
        principal: PrincipalParameter,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        repository = authorized(principal)
        await repository.get_run(run_id)
        return StreamingResponse(
            _stream_run(request, repository, run_id, _stream_cursor(request, after)),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/api/ag-ui")
    async def run_agui(
        payload: RunAgentInput,
        request: Request,
        principal: PrincipalParameter,
    ) -> StreamingResponse:
        repository = authorized(principal)
        try:
            thread_id = UUID(payload.thread_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="threadId must be a UUID") from error
        prompt = _prompt_from_agui(payload)
        existing = None
        if payload.run_id is not None:
            try:
                existing = await repository.get_run_by_agui_id(thread_id, payload.run_id)
            except NotFoundError:
                pass
        if existing is None:
            run = await repository.create_run(
                thread_id,
                prompt,
                agui_run_id=payload.run_id,
                input_payload=payload.model_dump(by_alias=True),
            )
        else:
            run = existing
        return StreamingResponse(
            _stream_run(request, repository, run["id"], _stream_cursor(request, 0)),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    return router
