from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from efferva import Efferva, Principal, Tool, UnauthenticatedError


async def resolve_header_principal(request: Request) -> Principal:
    subject = request.headers.get("x-test-user")
    if subject is None:
        raise UnauthenticatedError("missing test identity")
    return Principal(tenant_id="acme", issuer="tests", subject=subject)


def test_efferva_installs_under_product_prefix_and_reuses_product_title() -> None:
    app = FastAPI(title="Product Host")
    frame = Efferva(identity=resolve_header_principal)
    frame.install(app, prefix="/agent")
    client = TestClient(app, raise_server_exceptions=False)

    unauthorized = client.get("/agent/api/me")
    assert unauthorized.status_code == 401

    me = client.get("/agent/api/me", headers={"x-test-user": "alice"})
    assert me.status_code == 200
    assert me.json() == {
        "tenant_id": "acme",
        "issuer": "tests",
        "subject": "alice",
        "capabilities": [],
    }

    meta = client.get("/agent/api/meta", headers={"x-test-user": "alice"})
    assert meta.json() == {"title": "Product Host"}

    index = client.get("/agent/", headers={"x-test-user": "alice"})
    assert index.status_code == 200
    assert 'src="static/app.js?v=2"' in index.text

    static_asset = client.get("/agent/static/app.js")
    assert static_asset.status_code == 200
    assert 'endpoint("/api/me")' not in static_asset.text


def test_efferva_rejects_duplicate_application_tool_names() -> None:
    async def handler(*_: object) -> str:
        return "ok"

    tool = Tool(
        name="duplicate",
        description="A duplicate tool.",
        input_schema={"type": "object"},
        handler=handler,
    )

    with pytest.raises(ValueError, match="tool names must be unique"):
        Efferva(identity=resolve_header_principal, tools=[tool, tool])
