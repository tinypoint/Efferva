from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from efferva import Efferva, Principal
from efferva.config import get_settings
from efferva.db import Database
from semantic_alpha.reports import create_report_runs_router
from semantic_alpha.scheduling import (
    ReportScheduler,
    create_report_tasks_router,
)
from semantic_alpha.sandbox import create_sandbox_provider


async def resolve_local_principal(_: Request) -> Principal:
    return Principal(
        tenant_id="local",
        issuer="semantic-alpha",
        subject="developer",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database = Database(get_settings().database_url)
    await database.open()
    scheduler: ReportScheduler | None = None
    try:
        schema = files("semantic_alpha").joinpath("schema.sql").read_text()
        await database.initialize(schema)
        app.state.report_database = database
        app.state.report_owner_user_id = "developer"
        scheduler = ReportScheduler(
            app=app,
            sandbox=sandbox_provider,
            database=database,
        )
        await scheduler.start()
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()
        app.state.report_database = None
        app.state.report_owner_user_id = None
        await database.close()


app = FastAPI(title="Semantic Alpha", lifespan=lifespan)
static_dir = files("semantic_alpha").joinpath("static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(str(static_dir.joinpath("index.html")))


app.mount(
    "/assets",
    StaticFiles(directory=str(static_dir.joinpath("assets"))),
    name="assets",
)


sandbox_provider = create_sandbox_provider()
app.include_router(create_report_runs_router())
app.include_router(create_report_tasks_router())

Efferva(
    identity=resolve_local_principal,
).install(app, prefix="/agent")


@app.get("/{spa_path:path}", include_in_schema=False)
async def spa_fallback(spa_path: str) -> FileResponse:
    del spa_path
    return FileResponse(str(static_dir.joinpath("index.html")))
