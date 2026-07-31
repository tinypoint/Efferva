from importlib.resources import files

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from efferva import Efferva, Principal
from efferva.sandbox.providers.opensandbox import OpenSandboxProvider


async def resolve_local_principal(_: Request) -> Principal:
    return Principal(
        tenant_id="local",
        issuer="basic-local-docker",
        subject="developer",
    )


app = FastAPI(title="Efferva Basic Local Docker")
static_dir = files("basic_local_docker").joinpath("static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(str(static_dir.joinpath("index.html")))


@app.get("/assets/{asset_name}", include_in_schema=False)
async def asset(asset_name: str) -> FileResponse:
    if asset_name not in {"app.js", "style.css"}:
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(str(static_dir.joinpath(asset_name)))


Efferva(
    identity=resolve_local_principal,
    sandbox=OpenSandboxProvider(),
).install(app, prefix="/agent")
