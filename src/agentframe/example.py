"""Development-only application used by the bundled CLI and deployment smoke tests."""

from fastapi import Request

from agentframe import AgentFrame, Capability, Principal

_DEMO_PRINCIPALS = {
    "alice": Principal(tenant_id="acme", issuer="agentframe:demo", subject="alice"),
    "bob": Principal(tenant_id="acme", issuer="agentframe:demo", subject="bob"),
    "admin": Principal(
        tenant_id="acme",
        issuer="agentframe:demo",
        subject="admin",
        capabilities=frozenset({Capability.SESSIONS_READ_TENANT}),
    ),
    "other-admin": Principal(
        tenant_id="globex",
        issuer="agentframe:demo",
        subject="other-admin",
        capabilities=frozenset(
            {
                Capability.SESSIONS_READ_TENANT,
                Capability.SESSIONS_WRITE_TENANT,
            }
        ),
    ),
}


async def resolve_demo_principal(request: Request) -> Principal:
    """Resolve a deterministic fake user; never use this adapter in production."""

    name = request.headers.get("x-agentframe-demo-user")
    name = name or request.cookies.get("agentframe_demo_user") or "alice"
    return _DEMO_PRINCIPALS.get(name, _DEMO_PRINCIPALS["alice"])


app = AgentFrame(identity=resolve_demo_principal).asgi_app()
