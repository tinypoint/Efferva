from importlib.resources import files

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from efferva import Efferva, Principal
from efferva.sandbox.providers.opensandbox import OpenSandboxProvider


async def resolve_local_principal(_: Request) -> Principal:
    return Principal(
        tenant_id="local",
        issuer="semantic-alpha",
        subject="developer",
    )


app = FastAPI(title="Semantic Alpha")
static_dir = files("semantic_alpha").joinpath("static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(str(static_dir.joinpath("index.html")))


app.mount(
    "/assets",
    StaticFiles(directory=str(static_dir.joinpath("assets"))),
    name="assets",
)


Efferva(
    identity=resolve_local_principal,
    sandbox=OpenSandboxProvider(),
).install(app, prefix="/agent")


@app.get("/{spa_path:path}", include_in_schema=False)
async def spa_fallback(spa_path: str) -> FileResponse:
    del spa_path
    return FileResponse(str(static_dir.joinpath("index.html")))
