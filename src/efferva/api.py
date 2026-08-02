from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from efferva.broker import RedisRunBroker, RunQueueFullError
from efferva.codex_projection import project_turn_messages
from efferva.identity import IdentityResolver, Principal
from efferva.models import (
    PrincipalView,
    RunAgentInput,
    PromptInput,
    Session,
    SessionCreate,
    ThreadCreate,
)
from efferva.repository import (
    AccessMode,
    RunRepository,
    SessionRepository,
)
from efferva.runtime import CodexProxy, CodexRpcError


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
            if any(
                part.get("type") in {"image", "audio"}
                for part in message.content
            ):
                return ""
    raise HTTPException(status_code=422, detail="AG-UI input requires a user message")


def _media_inputs_from_agui(input_payload: RunAgentInput) -> list[dict[str, Any]]:
    for message in reversed(input_payload.messages):
        if message.role != "user" or not isinstance(message.content, list):
            continue
        inputs: list[dict[str, Any]] = []
        for part in message.content:
            media_type = str(part.get("type") or "")
            if media_type not in {"image", "audio"}:
                continue
            source = part.get("source")
            if not isinstance(source, dict):
                continue
            value = str(source.get("value") or "")
            if not value:
                continue
            if source.get("type") == "data":
                mime_type = str(source.get("mimeType") or "application/octet-stream")
                value = f"data:{mime_type};base64,{value}"
            inputs.append({"type": media_type, "url": value})
        return inputs
    return []


def _sse_event(event_id: str, event: dict[str, Any]) -> str:
    return (
        f"id: {event_id}\n"
        f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
    )


async def _brokered_stream(
    broker: RedisRunBroker,
    run_id: str,
    *,
    after: str = "0-0",
) -> AsyncIterator[str]:
    async for event_id, event in broker.stream_events(run_id, after=after):
        yield _sse_event(event_id, event)


def _brokered_response(
    broker: RedisRunBroker,
    run_id: str,
    *,
    after: str = "0-0",
) -> StreamingResponse:
    return StreamingResponse(
        _brokered_stream(broker, run_id, after=after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def create_api_router(
    *,
    identity: IdentityResolver,
    repository: Callable[[], SessionRepository],
    codex_proxy: Callable[[], CodexProxy],
    run_broker: Callable[[], RedisRunBroker],
    runs: Callable[[], RunRepository],
) -> APIRouter:
    router = APIRouter()
    resolve_principal = principal_dependency(identity)

    async def enqueue(command: dict[str, Any]) -> None:
        run_id = str(command["runId"])
        await runs().create(
            run_id,
            UUID(str(command["sessionId"])),
            str(command["threadId"]),
            command,
        )
        try:
            await run_broker().enqueue_run(command)
        except RunQueueFullError as error:
            await runs().update(run_id, status="failed", error=str(error))
            raise HTTPException(status_code=503, detail=str(error)) from error
        except ValueError as error:
            await runs().update(run_id, status="failed", error=str(error))
            raise HTTPException(status_code=413, detail=str(error)) from error
        except Exception as error:
            await runs().update(run_id, status="failed", error=str(error))
            raise

    async def validate_execution_settings(
        session: dict[str, Any],
        model: str,
        reasoning_effort: str | None = None,
    ) -> None:
        models = await codex_proxy().list_models(session)
        selected = next(
            (item for item in models if item.get("model") == model),
            None,
        )
        if selected is None:
            raise HTTPException(status_code=422, detail="unsupported model")
        if reasoning_effort is None:
            return
        efforts = {
            str(item.get("reasoningEffort"))
            for item in selected.get("supportedReasoningEfforts") or []
            if item.get("reasoningEffort")
        }
        if reasoning_effort not in efforts:
            raise HTTPException(
                status_code=422,
                detail="unsupported reasoning effort for this model",
            )

    async def load_thread(
        session: dict[str, Any],
        thread_id: str,
    ) -> dict[str, Any]:
        try:
            return await codex_proxy().read_thread(session, thread_id)
        except CodexRpcError as error:
            message = str(error.error.get("message") or "").lower()
            if "not found" in message or "no rollout found" in message:
                raise HTTPException(
                    status_code=404,
                    detail=f"thread {thread_id} not found",
                ) from error
            raise

    async def load_turn_page(
        session: dict[str, Any],
        thread_id: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        try:
            page = await codex_proxy().list_thread_turns(
                session,
                thread_id,
                cursor=cursor,
                limit=limit,
                sort_direction="desc",
                items_view="full",
            )
        except CodexRpcError as error:
            message = str(error.error.get("message") or "")
            if "is not materialized yet" not in message:
                raise
            return [], None
        turns = [dict(turn) for turn in reversed(page.get("data") or [])]
        next_cursor = page.get("nextCursor")
        return turns, next_cursor if isinstance(next_cursor, str) else None

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        await repository().ping()
        await run_broker().ping()
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
        return threads

    @router.get("/api/sessions/{session_id}/models")
    async def list_models(
        session_id: UUID,
        principal: Principal = Depends(resolve_principal),
    ) -> list[dict[str, Any]]:
        session = await repository().get_session(principal, session_id, touch=True)
        return await codex_proxy().list_models(session)

    @router.get("/api/sessions/{session_id}/skills")
    async def list_skills(
        session_id: UUID,
        workspace: str | None = Query(default=None),
        principal: Principal = Depends(resolve_principal),
    ) -> list[dict[str, Any]]:
        if workspace is not None and not workspace.startswith("/"):
            raise HTTPException(
                status_code=422,
                detail="workspace must be an absolute Sandbox path",
            )
        session = await repository().get_session(principal, session_id, touch=True)
        return await codex_proxy().list_skills(session, workspace=workspace)

    @router.get("/api/sessions/{session_id}/files")
    async def search_files(
        session_id: UUID,
        query: str = Query(default="", max_length=256),
        workspace: str | None = Query(default=None),
        principal: Principal = Depends(resolve_principal),
    ) -> list[dict[str, Any]]:
        if workspace is not None and not workspace.startswith("/"):
            raise HTTPException(
                status_code=422,
                detail="workspace must be an absolute Sandbox path",
            )
        session = await repository().get_session(principal, session_id, touch=True)
        return await codex_proxy().search_files(
            session,
            query,
            workspace=workspace,
        )

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
            workspace=payload.workspace,
            model=payload.model,
            reasoning_effort=payload.reasoning_effort,
            dynamic_tools=payload.tools,
        )
        return thread

    @router.get("/api/sessions/{session_id}/threads/{thread_id}")
    async def read_thread(
        session_id: UUID,
        thread_id: str,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        session = await repository().get_session(principal, session_id, touch=True)
        return await load_thread(session, thread_id)

    @router.get("/api/sessions/{session_id}/threads/{thread_id}/turns")
    async def list_thread_turns(
        session_id: UUID,
        thread_id: str,
        cursor: str | None = Query(default=None),
        limit: int | None = Query(default=None, ge=1),
        sort_direction: Literal["asc", "desc"] | None = Query(
            default=None,
            alias="sortDirection",
        ),
        items_view: Literal["notLoaded", "summary", "full"] | None = Query(
            default=None,
            alias="itemsView",
        ),
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        session = await repository().get_session(principal, session_id, touch=True)
        return await codex_proxy().list_thread_turns(
            session,
            thread_id,
            cursor=cursor,
            limit=limit,
            sort_direction=sort_direction,
            items_view=items_view,
        )

    @router.get("/api/sessions/{session_id}/threads/{thread_id}/ag-ui")
    async def read_thread_agui(
        session_id: UUID,
        thread_id: str,
        cursor: str | None = Query(default=None),
        limit: int = Query(default=20, ge=1, le=100),
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        session = await repository().get_session(principal, session_id, touch=True)
        if cursor is None:
            _, execution_settings, turn_page = await asyncio.gather(
                load_thread(session, thread_id),
                codex_proxy().get_thread_settings(session, thread_id),
                load_turn_page(
                    session,
                    thread_id,
                    cursor=None,
                    limit=limit,
                ),
            )
            turns, next_cursor = turn_page
        else:
            execution_settings = None
            turns, next_cursor = await load_turn_page(
                session,
                thread_id,
                cursor=cursor,
                limit=limit,
            )
        detail: dict[str, Any] = {
            "messages": project_turn_messages(turns),
            "next_cursor": next_cursor,
        }
        if cursor is not None:
            return detail
        detail.update(execution_settings)
        active_turn = next(
            (
                turn
                for turn in reversed(turns)
                if turn.get("status") == "inProgress"
            ),
            None,
        )
        active_turn_id = (
            str(active_turn["id"])
            if active_turn is not None
            else None
        )
        detail["active_turn_id"] = active_turn_id
        if active_turn_id:
            active_run = await runs().find_by_turn(
                session_id,
                thread_id,
                str(active_turn_id),
            )
            if active_run is not None and active_run.get("started_at") is not None:
                detail["active_turn_started_at"] = active_run["started_at"].isoformat()
        latest_run = await runs().find_latest_for_thread(session_id, thread_id)
        if (
            latest_run is not None
            and latest_run.get("status") == "failed"
        ):
            detail["last_run_error"] = str(
                latest_run.get("error") or "Run failed"
            )
        return detail

    @router.delete("/api/sessions/{session_id}/threads/{thread_id}")
    async def delete_thread(
        session_id: UUID,
        thread_id: str,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, bool]:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        await codex_proxy().delete_thread(session, thread_id)
        return {"deleted": True}

    @router.get("/api/sessions/{session_id}/threads/{thread_id}/resume")
    async def resume_thread(
        session_id: UUID,
        thread_id: str,
        request: Request,
        turn_id: str = Query(min_length=1),
        principal: Principal = Depends(resolve_principal),
    ) -> StreamingResponse:
        session = await repository().get_session(principal, session_id, touch=True)
        existing = await runs().find_by_turn(session_id, thread_id, turn_id)
        if existing is not None and existing["status"] in {
            "queued",
            "running",
            "waiting_input",
        }:
            after = request.headers.get("last-event-id", "0-0")
            return _brokered_response(run_broker(), str(existing["id"]), after=after)
        run_id = str(uuid4())
        await enqueue(
            {
                "kind": "resume",
                "runId": run_id,
                "sessionId": str(session["id"]),
                "threadId": thread_id,
                "turnId": turn_id,
            }
        )
        return _brokered_response(run_broker(), run_id)

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
        run_id = await run_broker().find_run(str(session["id"]), thread_id, turn_id)
        if run_id is None:
            raise HTTPException(status_code=409, detail="active run is not owned by a Worker")
        run = await runs().get(run_id, session_id)
        if run["status"] != "running":
            raise HTTPException(status_code=409, detail="turn is not running")
        await run_broker().send_command(
            run_id,
            {
                "kind": "interrupt",
                "sessionId": str(session["id"]),
                "threadId": thread_id,
                "turnId": turn_id,
            },
        )
        return {"interrupted": True}

    @router.post(
        "/api/sessions/{session_id}/threads/{thread_id}/turns/{turn_id}/steer"
    )
    async def steer_turn(
        session_id: UUID,
        thread_id: str,
        turn_id: str,
        payload: PromptInput,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, str]:
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        run_id = await run_broker().find_run(str(session["id"]), thread_id, turn_id)
        if run_id is None:
            raise HTTPException(status_code=409, detail="active run is not owned by a Worker")
        run = await runs().get(run_id, session_id)
        if run["status"] != "running":
            raise HTTPException(status_code=409, detail="turn is not running")
        await run_broker().send_command(
            run_id,
            {
                "kind": "steer",
                "sessionId": str(session["id"]),
                "threadId": thread_id,
                "turnId": turn_id,
                "prompt": payload.prompt,
            },
        )
        return {"turnId": turn_id}

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
        if payload.resume is not None:
            pending = await run_broker().get_pending_interrupt(
                str(session["id"]),
                payload.thread_id,
            )
            if pending is None:
                raise HTTPException(
                    status_code=409,
                    detail="this thread has no pending Codex interrupt",
                )
            pending_run_id = str(pending["runId"])
            run = await runs().get(pending_run_id, session_id)
            if run["status"] not in {"running", "waiting_input"}:
                raise HTTPException(
                    status_code=409,
                    detail="the interrupted Codex run is no longer active",
                )
            await run_broker().send_command(
                pending_run_id,
                {"kind": "resume_interrupt", "responses": payload.resume},
            )
            return _brokered_response(
                run_broker(),
                pending_run_id,
                after=str(pending["eventId"]),
            )
        run_id = str(uuid4())
        client_run_id = payload.run_id or run_id
        prompt = _prompt_from_agui(payload)
        model = str(forwarded.get("model") or "").strip() or None
        reasoning_effort = (
            str(forwarded.get("reasoningEffort") or "").strip() or None
        )
        if model:
            await validate_execution_settings(session, model, reasoning_effort)
        workspace = str(forwarded.get("workspace") or "").strip() or None
        command, separator, argument = prompt.partition(" ")
        argument = argument.strip() if separator else ""
        if command == "/plan":
            if not argument:
                await enqueue(
                    {
                        "kind": "control",
                        "action": "plan.enable",
                        "runId": run_id,
                        "clientRunId": client_run_id,
                        "sessionId": str(session["id"]),
                        "threadId": payload.thread_id,
                        "model": model,
                        "reasoningEffort": reasoning_effort,
                    }
                )
                return _brokered_response(run_broker(), run_id)
            await enqueue(
                {
                    "kind": "start",
                    "runId": run_id,
                    "clientRunId": client_run_id,
                    "sessionId": str(session["id"]),
                    "threadId": payload.thread_id,
                    "prompt": argument,
                    "model": model,
                    "reasoningEffort": reasoning_effort,
                    "collaborationMode": "plan",
                    "workspace": workspace,
                    "tools": payload.tools,
                }
            )
            return _brokered_response(
                run_broker(),
                run_id,
                after="0-0",
            )
        if command == "/goal":
            action = "goal.get"
            control: dict[str, Any] = {}
            if argument == "clear":
                action = "goal.clear"
            elif argument in {"pause", "resume"}:
                action = "goal.status"
                control["status"] = "paused" if argument == "pause" else "active"
            elif argument:
                action = "goal.set"
                control["objective"] = (
                    argument.removeprefix("edit ").strip()
                    if argument.startswith("edit ")
                    else argument
                )
            await enqueue(
                {
                    "kind": "control",
                    "action": action,
                    "runId": run_id,
                    "clientRunId": client_run_id,
                    "sessionId": str(session["id"]),
                    "threadId": payload.thread_id,
                    **control,
                }
            )
            return _brokered_response(run_broker(), run_id)
        await enqueue(
            {
                "kind": "start",
                "runId": run_id,
                "clientRunId": client_run_id,
                "sessionId": str(session["id"]),
                "threadId": payload.thread_id,
                "prompt": prompt,
                "model": model,
                "reasoningEffort": reasoning_effort,
                "workspace": workspace,
                "tools": payload.tools,
                "inputs": _media_inputs_from_agui(payload),
            }
        )
        return _brokered_response(
            run_broker(),
            run_id,
            after="0-0",
        )

    return router
