"""Tiny cookie login used only to demonstrate integration with a product identity system."""

from dataclasses import dataclass

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from agentframe import Capability, Principal, UnauthenticatedError


@dataclass(frozen=True, slots=True)
class DemoUser:
    user_id: str
    organization_id: str
    capabilities: frozenset[Capability] = frozenset()


USERS = {
    "alice": DemoUser("alice", "acme"),
    "bob": DemoUser("bob", "acme"),
    "admin": DemoUser(
        "admin",
        "acme",
        frozenset({Capability.SESSIONS_READ_TENANT}),
    ),
    "other-admin": DemoUser(
        "other-admin",
        "globex",
        frozenset(
            {
                Capability.SESSIONS_READ_TENANT,
                Capability.SESSIONS_WRITE_TENANT,
            }
        ),
    ),
}


class DemoAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        request.state.user = USERS.get(request.cookies.get("basic_product_user", ""))
        return await call_next(request)


async def resolve_principal(request: Request) -> Principal:
    user: DemoUser | None = request.state.user
    if user is None:
        raise UnauthenticatedError("请先登录示例产品")
    return Principal(
        tenant_id=user.organization_id,
        issuer="basic-product",
        subject=user.user_id,
        capabilities=user.capabilities,
    )


router = APIRouter()


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request) -> str:
    current = request.state.user
    links = "".join(f'<li><a href="/login/{name}">{name}</a></li>' for name in USERS)
    current_text = current.user_id if current else "未登录"
    return f"""
    <!doctype html>
    <html lang="zh-CN">
      <head><meta charset="utf-8"><title>Basic Product</title></head>
      <body style="font-family: sans-serif; max-width: 640px; margin: 60px auto">
        <h1>Basic Product</h1>
        <p>当前身份：{current_text}</p>
        <ul>{links}</ul>
        <p><a href="/agent/">进入 AgentFrame WebUI</a> · <a href="/logout">退出</a></p>
        <small>这是开发示例，不是生产登录实现。</small>
      </body>
    </html>
    """


@router.get("/login/{name}", include_in_schema=False)
async def login(name: str) -> RedirectResponse:
    if name not in USERS:
        return RedirectResponse("/", status_code=303)
    response = RedirectResponse("/agent/", status_code=303)
    response.set_cookie(
        "basic_product_user",
        name,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout", include_in_schema=False)
async def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("basic_product_user")
    return response
