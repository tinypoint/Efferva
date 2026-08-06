from importlib.resources import files

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from efferva import Efferva, Principal
from efferva.sandbox.providers.opensandbox import (
    OpenSandboxConnectionConfig,
    OpenSandboxCreateContext,
    OpenSandboxCreateSpec,
    OpenSandboxProvider,
)
from multi_session.configuration import InstanceConfig, load_config


async def resolve_local_principal(_: Request) -> Principal:
    return Principal(
        tenant_id="local",
        issuer="multi-session",
        subject="developer",
    )


def sandbox_provider(
    instance: InstanceConfig,
    connection: OpenSandboxConnectionConfig,
) -> OpenSandboxProvider:
    async def resolve_spec(
        _: OpenSandboxCreateContext,
    ) -> OpenSandboxCreateSpec:
        return instance.sandbox_spec

    return OpenSandboxProvider(
        connection,
        layout=instance.efferva.sandbox,
        resolve_spec=resolve_spec,
    )


app = FastAPI(title="Efferva Multi-session")
static_dir = files("multi_session").joinpath("static")
config = load_config()

Efferva(
    config=config.codex.efferva,
    identity=resolve_local_principal,
    sandbox=sandbox_provider(config.codex, config.opensandbox),
    engine=config.codex.engine,
).install(app, prefix="/codex")

Efferva(
    config=config.claude.efferva,
    identity=resolve_local_principal,
    sandbox=sandbox_provider(config.claude, config.opensandbox),
    engine=config.claude.engine,
).install(app, prefix="/claude")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(str(static_dir.joinpath("index.html")))


app.mount(
    "/assets",
    StaticFiles(directory=str(static_dir.joinpath("assets")), check_dir=False),
    name="assets",
)


@app.get("/{spa_path:path}", include_in_schema=False)
async def spa_fallback(spa_path: str) -> FileResponse:
    del spa_path
    return FileResponse(str(static_dir.joinpath("index.html")))
