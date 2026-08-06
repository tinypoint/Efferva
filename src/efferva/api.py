from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request

from efferva.identity import (
    IdentityResolver,
    Principal,
)
from efferva.models import PrincipalView, Session, SessionCreate
from efferva.session_repository import (
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


def create_api_router(
    *,
    identity: IdentityResolver,
    repository: SessionRepository,
    engine_name: str,
    engine_protocol: str,
) -> APIRouter:
    router = APIRouter()
    resolve_principal = principal_dependency(identity)

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        await repository.ping()
        return {"status": "ok"}

    @router.get("/api/meta", include_in_schema=False)
    async def metadata(
        request: Request,
        _: Principal = Depends(resolve_principal),
    ) -> dict[str, str]:
        return {
            "title": request.app.title,
            "engine": engine_name,
            "protocol": engine_protocol,
        }

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
        return await repository.create_session(principal, payload.name)

    @router.get("/api/sessions", response_model=list[Session])
    async def list_sessions(
        principal: Principal = Depends(resolve_principal),
        scope: Literal["mine", "tenant"] = Query(default="mine"),
    ) -> list[dict[str, Any]]:
        return await repository.list_sessions(principal, scope)

    return router
