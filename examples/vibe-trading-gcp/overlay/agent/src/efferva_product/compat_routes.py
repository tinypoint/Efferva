"""Keep the existing Vibe-Trading session UI while Efferva owns the control plane."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, StreamingResponse
from src.api.sessions_routes import (
    CreateSessionRequest,
    MessageResponse,
    SendMessageRequest,
    SessionResponse,
)
from src.efferva_product.identity import resolve_principal

_IDENTITY_HEADERS = ("cookie", "x-goog-iap-jwt-assertion")


def _forwarded_identity_headers(request: Request) -> dict[str, str]:
    return {name: request.headers[name] for name in _IDENTITY_HEADERS if name in request.headers}


class _EffervaApi:
    def __init__(self, app: FastAPI) -> None:
        self._transport = httpx.ASGITransport(app=app)

    async def request(
        self,
        incoming: Request,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(
            transport=self._transport,
            base_url="http://efferva.internal",
        ) as client:
            response = await client.request(
                method,
                f"/efferva/api{path}",
                headers=_forwarded_identity_headers(incoming),
                json=payload,
            )
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise HTTPException(status_code=response.status_code, detail=detail)
        if not response.content:
            return None
        return response.json()


def _session_response(
    session: dict[str, Any],
    last_attempt_id: str | None = None,
) -> dict[str, Any]:
    return {
        "session_id": session["id"],
        "title": session["name"],
        "status": session["status"],
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
        "last_attempt_id": last_attempt_id,
    }


def _sse(event_id: str, event_type: str, data: dict[str, Any]) -> str:
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event_id}\nevent: {event_type}\ndata: {encoded}\n\n"


async def _primary_thread(
    api: _EffervaApi,
    request: Request,
    session_id: str,
    *,
    create: bool,
) -> dict[str, Any] | None:
    threads = await api.request(request, "GET", f"/sessions/{session_id}/threads")
    if threads:
        return threads[0]
    if not create:
        return None
    return await api.request(
        request,
        "POST",
        f"/sessions/{session_id}/threads",
        payload={"title": "Vibe Trading"},
    )


def _latest_run(detail: dict[str, Any]) -> dict[str, Any] | None:
    runs = detail.get("runs") or []
    if not runs:
        return None
    return max(runs, key=lambda run: run["created_at"])


def _assistant_summary(detail: dict[str, Any], run_id: str) -> str:
    messages = [
        message
        for message in detail.get("messages") or []
        if message.get("role") == "assistant" and message.get("run_id") == run_id
    ]
    return messages[-1]["content"] if messages else ""


async def _event_stream(
    api: _EffervaApi,
    request: Request,
    session_id: str,
    last_event_id: str | None,
    replay_active: bool,
) -> AsyncIterator[str]:
    thread = await _primary_thread(api, request, session_id, create=False)
    if thread is None:
        return
    thread_id = thread["id"]
    detail = await api.request(request, "GET", f"/threads/{thread_id}")
    initial = _latest_run(detail)
    watched_run_id: str | None = None
    cursor = 0

    if last_event_id and ":" in last_event_id:
        watched_run_id, raw_cursor = last_event_id.rsplit(":", 1)
        cursor = int(raw_cursor) if raw_cursor.isdigit() else 0
    elif replay_active and initial and initial["status"] in {"queued", "running"}:
        watched_run_id = initial["id"]
    baseline_run_id = initial["id"] if initial else None
    announced = False

    while not await request.is_disconnected():
        detail = await api.request(request, "GET", f"/threads/{thread_id}")
        latest = _latest_run(detail)
        if watched_run_id is None and latest is not None and latest["id"] != baseline_run_id:
            watched_run_id = latest["id"]
            cursor = 0
        if watched_run_id is None:
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)
            continue

        if not announced:
            yield _sse(
                f"{watched_run_id}:created",
                "attempt.created",
                {"attempt_id": watched_run_id},
            )
            yield _sse(
                f"{watched_run_id}:started",
                "attempt.started",
                {"attempt_id": watched_run_id},
            )
            announced = True

        stored_events = await api.request(
            request,
            "GET",
            f"/runs/{watched_run_id}/events?after={cursor}",
        )
        for stored in stored_events:
            cursor = int(stored["seq"])
            event = stored["event"]
            event_id = f"{watched_run_id}:{cursor}"
            event_type = event.get("type")
            if event_type == "TEXT_MESSAGE_CONTENT":
                yield _sse(
                    event_id,
                    "text_delta",
                    {"delta": event.get("delta", ""), "attempt_id": watched_run_id},
                )
            elif event_type == "RUN_FINISHED":
                refreshed = await api.request(request, "GET", f"/threads/{thread_id}")
                yield _sse(
                    event_id,
                    "attempt.completed",
                    {
                        "attempt_id": watched_run_id,
                        "status": "completed",
                        "summary": _assistant_summary(refreshed, watched_run_id),
                    },
                )
                yield _sse(event_id, "done", {"attempt_id": watched_run_id})
                return
            elif event_type == "RUN_ERROR":
                yield _sse(
                    event_id,
                    "attempt.failed",
                    {
                        "attempt_id": watched_run_id,
                        "error": event.get("message", "Execution failed"),
                    },
                )
                yield _sse(event_id, "done", {"attempt_id": watched_run_id})
                return
        if not stored_events:
            yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)


def register_efferva_compat_routes(app: FastAPI) -> None:
    """Register the session contract consumed by the existing React application."""
    api = _EffervaApi(app)
    require_principal = Depends(resolve_principal)

    @app.get("/dev/login/{user}", include_in_schema=False)
    async def dev_login(user: str) -> RedirectResponse:
        from src.config.accessor import get_env_config

        if get_env_config().api.product_auth_mode.strip().lower() != "dev":
            raise HTTPException(status_code=404)
        if not user or len(user) > 64:
            raise HTTPException(status_code=422, detail="invalid development user")
        response = RedirectResponse("/agent", status_code=303)
        response.set_cookie("vibe_dev_user", user, httponly=True, samesite="lax")
        return response

    @app.post(
        "/sessions",
        response_model=SessionResponse,
        status_code=status.HTTP_201_CREATED,
        dependencies=[require_principal],
    )
    async def create_session(payload: CreateSessionRequest, request: Request):
        session = await api.request(
            request,
            "POST",
            "/sessions",
            payload={"name": payload.title or "New research"},
        )
        await _primary_thread(api, request, session["id"], create=True)
        return _session_response(session)

    @app.get(
        "/sessions",
        response_model=list[SessionResponse],
        dependencies=[require_principal],
    )
    async def list_sessions(request: Request, limit: int = Query(50, ge=1, le=200)):
        sessions = await api.request(request, "GET", "/sessions?scope=mine")
        return [_session_response(session) for session in sessions[:limit]]

    @app.get(
        "/sessions/{session_id}",
        response_model=SessionResponse,
        dependencies=[require_principal],
    )
    async def get_session(session_id: str, request: Request):
        session = await api.request(request, "GET", f"/sessions/{session_id}")
        thread = await _primary_thread(api, request, session_id, create=False)
        latest = None
        if thread is not None:
            detail = await api.request(request, "GET", f"/threads/{thread['id']}")
            latest = _latest_run(detail)
        return _session_response(session, latest["id"] if latest else None)

    @app.post(
        "/sessions/{session_id}/messages",
        dependencies=[require_principal],
    )
    async def send_message(
        session_id: str,
        payload: SendMessageRequest,
        request: Request,
    ):
        thread = await _primary_thread(api, request, session_id, create=True)
        run = await api.request(
            request,
            "POST",
            f"/threads/{thread['id']}/runs",
            payload={"prompt": payload.content},
        )
        return {"message_id": run["id"], "attempt_id": run["id"]}

    @app.get(
        "/sessions/{session_id}/messages",
        response_model=list[MessageResponse],
        dependencies=[require_principal],
    )
    async def get_messages(
        session_id: str,
        request: Request,
        limit: int = Query(100, ge=1, le=1000),
    ):
        thread = await _primary_thread(api, request, session_id, create=False)
        if thread is None:
            return []
        detail = await api.request(request, "GET", f"/threads/{thread['id']}")
        messages = detail.get("messages") or []
        return [
            {
                "message_id": message["id"],
                "session_id": session_id,
                "role": message["role"],
                "content": message["content"],
                "created_at": message["created_at"],
                "linked_attempt_id": message.get("run_id"),
                # Efferva Run IDs are control-plane IDs, not Vibe artifact run
                # directories. Artifact cards are added only after a real
                # artifact projection contract exists.
                "metadata": None,
            }
            for message in messages[-limit:]
        ]

    @app.get(
        "/sessions/{session_id}/events",
        dependencies=[require_principal],
    )
    async def session_events(
        session_id: str,
        request: Request,
        last_event_id: str | None = Query(None, alias="Last-Event-ID"),
        replay: str | None = Query(None),
    ):
        header_event_id = request.headers.get("Last-Event-ID")
        return StreamingResponse(
            _event_stream(
                api,
                request,
                session_id,
                header_event_id or last_event_id,
                replay_active=(replay or "").lower() == "active",
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/sessions/{session_id}/goal", dependencies=[require_principal])
    async def get_goal(session_id: str) -> None:
        raise HTTPException(status_code=404, detail=f"No current goal for {session_id}")

    @app.post("/sessions/{session_id}/cancel", dependencies=[require_principal])
    async def cancel_session(session_id: str) -> dict[str, str]:
        raise HTTPException(
            status_code=501,
            detail=f"Efferva run cancellation is not implemented for {session_id}",
        )

    @app.delete("/sessions/{session_id}", dependencies=[require_principal])
    async def delete_session(session_id: str) -> None:
        raise HTTPException(
            status_code=501,
            detail=f"Efferva session deletion is not implemented for {session_id}",
        )

    @app.patch("/sessions/{session_id}", dependencies=[require_principal])
    async def update_session(session_id: str) -> None:
        raise HTTPException(
            status_code=501,
            detail=f"Efferva session rename is not implemented for {session_id}",
        )
