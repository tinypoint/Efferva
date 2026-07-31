from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from efferva import Efferva, Principal
from efferva.sandbox.providers.opensandbox import OpenSandboxProvider


async def resolve_local_principal(_: Request) -> Principal:
    return Principal(
        tenant_id="local",
        issuer="basic-local-docker",
        subject="developer",
    )


app = FastAPI(title="Efferva Basic Local Docker")


@app.get("/", include_in_schema=False)
async def index() -> RedirectResponse:
    return RedirectResponse("/agent/")


Efferva(
    identity=resolve_local_principal,
    sandbox=OpenSandboxProvider(),
).install(app, prefix="/agent")
