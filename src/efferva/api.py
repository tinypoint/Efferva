from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from efferva.identity import IdentityResolver, Principal
from efferva.models import (
    PrincipalView,
    RunAgentInput,
    RunCreate,
    Session,
    SessionCreate,
    ThreadCreate,
)
from efferva.repository import AccessMode, SessionRepository
from efferva.runtime import CodexProxy


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
            prompt = "".join(
                part.get("text", "")
                for part in message.content
                if part.get("type") in {"text", "input_text"}
            )
            if prompt:
                return prompt
    raise HTTPException(status_code=422, detail="AG-UI input requires a user message")


def _sse(seq: int, event: dict[str, Any]) -> str:
    return f"id: {seq}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"


async def _agui_stream(
    proxy: CodexProxy,
    session: dict[str, Any],
    thread_id: str,
    prompt: str,
    *,
    run_id: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> AsyncIterator[str]:
    seq = 0
    message_id: str | None = None
    saw_delta = False
    yield _sse(
        seq := seq + 1,
        {
            "type": "RUN_STARTED",
            "runId": run_id,
            "threadId": thread_id,
            "input": {"prompt": prompt},
        },
    )
    try:
        async for notification in proxy.stream_turn(
            session,
            thread_id,
            prompt,
            model=model,
            reasoning_effort=reasoning_effort,
        ):
            method = notification["method"]
            params = notification.get("params") or {}
            if method == "efferva/turn-started":
                yield _sse(
                    seq := seq + 1,
                    {
                        "type": "RAW",
                        "event": notification,
                        "turnId": params.get("turnId"),
                    },
                )
                continue
            if method == "item/agentMessage/delta":
                item_id = str(params.get("itemId") or uuid4())
                if message_id is None:
                    message_id = f"{run_id}:{item_id}"
                    yield _sse(
                        seq := seq + 1,
                        {"type": "TEXT_MESSAGE_START", "messageId": message_id},
                    )
                delta = str(params.get("delta") or "")
                if delta:
                    saw_delta = True
                    yield _sse(
                        seq := seq + 1,
                        {
                            "type": "TEXT_MESSAGE_CONTENT",
                            "messageId": message_id,
                            "delta": delta,
                        },
                    )
                continue
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    item_id = str(item.get("id") or uuid4())
                    if message_id is None:
                        message_id = f"{run_id}:{item_id}"
                        yield _sse(
                            seq := seq + 1,
                            {"type": "TEXT_MESSAGE_START", "messageId": message_id},
                        )
                    text = str(item.get("text") or "")
                    if text and not saw_delta:
                        yield _sse(
                            seq := seq + 1,
                            {
                                "type": "TEXT_MESSAGE_CONTENT",
                                "messageId": message_id,
                                "delta": text,
                            },
                        )
                    yield _sse(
                        seq := seq + 1,
                        {"type": "TEXT_MESSAGE_END", "messageId": message_id},
                    )
                    message_id = None
                    saw_delta = False
                    continue
            if method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status")
                if message_id is not None:
                    yield _sse(
                        seq := seq + 1,
                        {"type": "TEXT_MESSAGE_END", "messageId": message_id},
                    )
                if status == "completed":
                    event = {
                        "type": "RUN_FINISHED",
                        "runId": run_id,
                        "threadId": thread_id,
                    }
                elif status in {"interrupted", "cancelled"}:
                    event = {"type": "RUN_CANCELLED", "runId": run_id}
                else:
                    error = turn.get("error") or {}
                    event = {
                        "type": "RUN_ERROR",
                        "code": "RUNTIME_ERROR",
                        "message": error.get("message") or f"turn {status}",
                    }
                yield _sse(seq := seq + 1, event)
                return
            yield _sse(
                seq := seq + 1,
                {"type": "RAW", "event": notification},
            )
    except Exception as error:
        yield _sse(
            seq := seq + 1,
            {
                "type": "RUN_ERROR",
                "code": "RUNTIME_ERROR",
                "message": str(error),
            },
        )


def create_api_router(
    *,
    identity: IdentityResolver,
    repository: Callable[[], SessionRepository],
    codex_proxy: Callable[[], CodexProxy],
) -> APIRouter:
    router = APIRouter()
    resolve_principal = principal_dependency(identity)

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        await repository().ping()
        if not codex_proxy().healthy:
            raise HTTPException(status_code=503, detail="Codex proxy is not healthy")
        return {"status": "ok"}

    @router.get("/api/meta", include_in_schema=False)
    async def metadata(
        request: Request,
        _: Principal = Depends(resolve_principal),
    ) -> dict[str, str]:
        return {"title": request.app.title}

    @router.get("/api/me", response_model=PrincipalView)
    async def me(
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        return {
            "tenant_id": principal.tenant_id,
            "issuer": principal.issuer,
            "subject": principal.subject,
            "capabilities": sorted(principal.capabilities, key=lambda item: item.value),
        }

    @router.post("/api/sessions", response_model=Session, status_code=201)
    async def create_session(
        payload: SessionCreate,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        return await repository().create_session(principal, payload.name)

    @router.get("/api/sessions", response_model=list[Session])
    async def list_sessions(
        principal: Principal = Depends(resolve_principal),
        scope: Literal["mine", "tenant"] = Query(default="mine"),
    ) -> list[dict[str, Any]]:
        return await repository().list_sessions(principal, scope)

    @router.get("/api/sessions/{session_id}", response_model=Session)
    async def get_session(
        session_id: UUID,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        return await repository().get_session(principal, session_id)

    @router.get("/api/sessions/{session_id}/threads")
    async def list_threads(
        session_id: UUID,
        principal: Principal = Depends(resolve_principal),
    ) -> list[dict[str, Any]]:
        session = await repository().get_session(principal, session_id, touch=True)
        threads = await codex_proxy().list_threads(session)
        return [_thread_summary(thread, session_id) for thread in threads]

    @router.post("/api/sessions/{session_id}/threads", status_code=201)
    async def create_thread(
        session_id: UUID,
        payload: ThreadCreate,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        thread = await codex_proxy().start_thread(
            session,
            title=payload.title,
            workspace=payload.workspace,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
        )
        return _thread_summary(thread, session_id)

    @router.get("/api/sessions/{session_id}/threads/{thread_id}")
    async def read_thread(
        session_id: UUID,
        thread_id: str,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        session = await repository().get_session(principal, session_id, touch=True)
        thread = await codex_proxy().read_thread(session, thread_id)
        return _thread_detail(thread, session_id)

    @router.post("/api/sessions/{session_id}/threads/{thread_id}/turns")
    async def start_turn(
        session_id: UUID,
        thread_id: str,
        payload: RunCreate,
        principal: Principal = Depends(resolve_principal),
    ) -> StreamingResponse:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        return StreamingResponse(
            _agui_stream(
                codex_proxy(),
                session,
                thread_id,
                payload.prompt,
                run_id=str(uuid4()),
                model=payload.model,
                reasoning_effort=payload.reasoning_effort,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post(
        "/api/sessions/{session_id}/threads/{thread_id}/turns/{turn_id}/interrupt"
    )
    async def interrupt_turn(
        session_id: UUID,
        thread_id: str,
        turn_id: str,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, bool]:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        await codex_proxy().interrupt_turn(session, thread_id, turn_id)
        return {"interrupted": True}

    @router.post("/api/ag-ui")
    async def run_agui(
        payload: RunAgentInput,
        principal: Principal = Depends(resolve_principal),
    ) -> StreamingResponse:
        forwarded = (
            payload.forwarded_props
            if isinstance(payload.forwarded_props, dict)
            else {}
        )
        raw_session_id = forwarded.get("sessionId")
        if not raw_session_id:
            raise HTTPException(
                status_code=422,
                detail="forwardedProps.sessionId is required",
            )
        try:
            session_id = UUID(str(raw_session_id))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="sessionId must be a UUID") from error
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        run_id = payload.run_id or str(uuid4())
        return StreamingResponse(
            _agui_stream(
                codex_proxy(),
                session,
                payload.thread_id,
                _prompt_from_agui(payload),
                run_id=run_id,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


def _thread_summary(thread: dict[str, Any], session_id: UUID) -> dict[str, Any]:
    return {
        "id": str(thread["id"]),
        "session_id": str(session_id),
        "title": thread.get("name") or thread.get("preview") or "Untitled thread",
        "workspace": thread.get("cwd"),
        "status": thread.get("status"),
        "created_at": thread.get("createdAt"),
        "updated_at": thread.get("updatedAt") or thread.get("createdAt"),
    }


def _thread_detail(thread: dict[str, Any], session_id: UUID) -> dict[str, Any]:
    summary = _thread_summary(thread, session_id)
    messages: list[dict[str, Any]] = []
    for turn in thread.get("turns") or []:
        for item in turn.get("items") or []:
            item_type = item.get("type")
            if item_type == "userMessage":
                content = item.get("content") or []
                text = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict)
                )
                messages.append(
                    {"id": item.get("id"), "role": "user", "content": text}
                )
            elif item_type == "agentMessage":
                messages.append(
                    {
                        "id": item.get("id"),
                        "role": "assistant",
                        "content": item.get("text") or "",
                    }
                )
    return {**summary, "messages": messages}
