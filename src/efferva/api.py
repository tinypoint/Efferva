from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from efferva.broker import RedisRunBroker, RunQueueFullError
from efferva.codex import CodexGateway
from efferva.codex_projection import project_turn_messages
from efferva.codex_rpc import CodexRpcError
from efferva.codex_tunnel import (
    CodexTunnelBackpressureError,
    CodexTunnelQueueFullError,
    RedisCodexTunnel,
)
from efferva.identity import (
    ForbiddenError,
    IdentityResolver,
    Principal,
    UnauthenticatedError,
)
from efferva.models import (
    CodexControlInput,
    PrincipalView,
    RunAgentInput,
    PromptInput,
    Session,
    SessionCreate,
)
from efferva.repository import (
    AccessMode,
    NotFoundError,
    RunRepository,
    SessionRepository,
)


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
    codex_gateway: Callable[[], CodexGateway],
    codex_tunnel: Callable[[], RedisCodexTunnel],
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

    async def load_thread(
        session: dict[str, Any],
        thread_id: str,
    ) -> dict[str, Any]:
        try:
            return await codex_gateway().read_thread(session, thread_id)
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
            page = await codex_gateway().list_thread_turns(
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
        await codex_tunnel().ping()
        return {"status": "ok"}

    @router.websocket("/api/sessions/{session_id}/codex")
    async def codex_websocket(
        websocket: WebSocket,
        session_id: UUID,
    ) -> None:
        try:
            principal = await identity(websocket)
            if not isinstance(principal, Principal):
                raise TypeError("IdentityResolver must return efferva.Principal")
            await repository().get_session(
                principal,
                session_id,
                mode=AccessMode.WRITE,
                touch=True,
            )
        except UnauthenticatedError:
            await websocket.close(code=4401)
            return
        except ForbiddenError:
            await websocket.close(code=4403)
            return
        except NotFoundError:
            await websocket.close(code=4404)
            return

        connection_id = str(uuid4())
        tunnel = codex_tunnel()
        try:
            await tunnel.open_connection(connection_id, str(session_id))
        except CodexTunnelQueueFullError:
            await websocket.close(code=1013, reason="Codex workers are busy")
            return
        await websocket.accept()

        async def client_heartbeat() -> None:
            while True:
                await asyncio.sleep(tunnel.heartbeat_interval_seconds)
                if not await tunnel.touch_client(connection_id):
                    return

        async def client_to_worker() -> None:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                payload = message.get("text")
                if payload is None:
                    await websocket.close(
                        code=1003,
                        reason="Codex JSON-RPC requires text frames",
                    )
                    return
                await tunnel.send_frame(
                    connection_id,
                    "client",
                    payload=payload,
                )

        async def worker_to_client() -> None:
            cursor = "0-0"
            while True:
                frames = await tunnel.read_frames(
                    connection_id,
                    "server",
                    after=cursor,
                )
                if not frames:
                    state = await tunnel.get_state(connection_id)
                    if state.get("status") in {"closed", "failed"}:
                        await websocket.close(
                            code=1011 if state.get("status") == "failed" else 1000,
                            reason=(
                                "Codex connection failed"
                                if state.get("status") == "failed"
                                else "Codex connection closed"
                            ),
                        )
                        return
                    continue
                for frame_id, kind, payload in frames:
                    cursor = frame_id
                    if kind == "close":
                        await tunnel.acknowledge_frame(
                            connection_id,
                            "server",
                            frame_id,
                        )
                        state = await tunnel.get_state(connection_id)
                        await websocket.close(
                            code=1011 if state.get("status") == "failed" else 1000,
                            reason=(
                                "Codex connection failed"
                                if state.get("status") == "failed"
                                else "Codex connection closed"
                            ),
                        )
                        return
                    await websocket.send_text(payload)
                    await tunnel.acknowledge_frame(
                        connection_id,
                        "server",
                        frame_id,
                    )

        tasks = {
            asyncio.create_task(client_heartbeat()),
            asyncio.create_task(client_to_worker()),
            asyncio.create_task(worker_to_client()),
        }
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except (CodexTunnelBackpressureError, ValueError, WebSocketDisconnect):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            with suppress(CodexTunnelBackpressureError):
                await tunnel.disconnect_client(connection_id)
            if websocket.client_state == WebSocketState.CONNECTED:
                with suppress(RuntimeError):
                    await websocket.close()

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

    @router.get("/api/sessions/{session_id}/threads")
    async def list_threads(
        session_id: UUID,
        principal: Principal = Depends(resolve_principal),
    ) -> list[dict[str, Any]]:
        session = await repository().get_session(principal, session_id, touch=True)
        threads = await codex_gateway().list_threads(session)
        return threads

    @router.get("/api/sessions/{session_id}/models")
    async def list_models(
        session_id: UUID,
        principal: Principal = Depends(resolve_principal),
    ) -> list[dict[str, Any]]:
        session = await repository().get_session(principal, session_id, touch=True)
        return await codex_gateway().list_models(session)

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
        return await codex_gateway().list_skills(session, workspace=workspace)

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
        return await codex_gateway().search_files(
            session,
            query,
            workspace=workspace,
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
                codex_gateway().get_thread_settings(session, thread_id),
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
        if active_turn is not None and active_turn.get("startedAt") is not None:
            detail["active_turn_started_at"] = active_turn["startedAt"]
        latest_turn = turns[-1] if turns else None
        if latest_turn is not None and latest_turn.get("status") == "failed":
            error = latest_turn.get("error") or {}
            detail["last_run_error"] = str(error.get("message") or "Turn failed")
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
        await codex_gateway().delete_thread(session, thread_id)
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
        await codex_gateway().interrupt_turn(session, thread_id, turn_id)
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
        steered_turn_id = await codex_gateway().steer_turn(
            session,
            thread_id,
            turn_id,
            payload.prompt,
        )
        return {"turnId": steered_turn_id}

    @router.post(
        "/api/sessions/{session_id}/threads/{thread_id}/controls"
    )
    async def run_control(
        session_id: UUID,
        thread_id: str,
        payload: CodexControlInput,
        principal: Principal = Depends(resolve_principal),
    ) -> dict[str, Any]:
        if thread_id == "new":
            raise HTTPException(
                status_code=409,
                detail="this control requires an existing thread",
            )
        session = await repository().get_session(
            principal,
            session_id,
            mode=AccessMode.WRITE,
            touch=True,
        )
        latest_run = await runs().find_latest_for_thread(session_id, thread_id)
        if latest_run is not None and latest_run["status"] in {
            "queued",
            "running",
            "waiting_input",
        }:
            raise HTTPException(
                status_code=409,
                detail="this thread has an active run",
            )

        collaboration_mode: str | None = None
        if payload.action == "plan.toggle":
            collaboration_mode = await codex_gateway().toggle_plan_mode(
                session,
                thread_id,
            )
            message = (
                "Plan mode enabled."
                if collaboration_mode == "plan"
                else "Plan mode disabled."
            )
        elif payload.action == "goal.get":
            goal = await codex_gateway().get_goal(session, thread_id)
            message = (
                f"Goal: {goal['objective']} ({goal['status']})"
                if goal
                else "No goal is set."
            )
        elif payload.action == "goal.clear":
            cleared = await codex_gateway().clear_goal(session, thread_id)
            message = "Goal cleared." if cleared else "No goal was set."
        elif payload.action == "goal.status":
            if payload.status is None:
                raise HTTPException(
                    status_code=422,
                    detail="goal.status requires status",
                )
            goal = await codex_gateway().set_goal(
                session,
                thread_id,
                status=payload.status,
            )
            message = f"Goal {goal['status']}: {goal['objective']}"
        elif payload.action == "goal.set":
            objective = (payload.objective or "").strip()
            if not objective:
                raise HTTPException(
                    status_code=422,
                    detail="goal.set requires an objective",
                )
            goal = await codex_gateway().set_goal(
                session,
                thread_id,
                objective=objective,
                status="active",
            )
            message = f"Goal set: {goal['objective']}"
        else:
            raise HTTPException(status_code=422, detail="unsupported control action")

        return {
            "action": payload.action,
            "message": message,
            "collaboration_mode": collaboration_mode,
        }

    @router.post("/api/sessions/{session_id}/ag-ui")
    async def run_agui(
        session_id: UUID,
        payload: RunAgentInput,
        principal: Principal = Depends(resolve_principal),
    ) -> StreamingResponse:
        forwarded = (
            payload.forwarded_props
            if isinstance(payload.forwarded_props, dict)
            else {}
        )
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
        workspace = str(forwarded.get("workspace") or "").strip() or None
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
