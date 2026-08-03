from importlib.resources import files

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from efferva import Efferva, Principal
from efferva.sandbox.providers.opensandbox import (
    OpenSandboxCreateContext,
    OpenSandboxCreateSpec,
    OpenSandboxProvider,
)
from multi_session_codex.configuration import load_config


async def resolve_local_principal(_: Request) -> Principal:
    return Principal(
        tenant_id="local",
        issuer="multi-session-codex",
        subject="developer",
    )


app = FastAPI(title="Efferva Multi-session Codex")
static_dir = files("multi_session_codex").joinpath("static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(str(static_dir.joinpath("index.html")))


app.mount(
    "/assets",
    StaticFiles(directory=str(static_dir.joinpath("assets")), check_dir=False),
    name="assets",
)

config = load_config()


async def resolve_sandbox_spec(_: OpenSandboxCreateContext) -> OpenSandboxCreateSpec:
    return config.sandbox_spec


sandbox_provider = OpenSandboxProvider(
    config.opensandbox_connection,
    layout=config.efferva.sandbox,
    resolve_spec=resolve_sandbox_spec,
)

Efferva(
    config=config.efferva,
    identity=resolve_local_principal,
    sandbox=sandbox_provider,
).install(app, prefix="/agent")


@app.get("/{spa_path:path}", include_in_schema=False)
async def spa_fallback(spa_path: str) -> FileResponse:
    del spa_path
    return FileResponse(str(static_dir.joinpath("index.html")))
