from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from efferva.api import create_api_router, principal_dependency
from efferva.config import Settings, get_settings
from efferva.db import Database
from efferva.identity import (
    ForbiddenError,
    IdentityResolver,
    Principal,
    UnauthenticatedError,
)
from efferva.repository import (
    ConflictError,
    NotFoundError,
    SystemRepository,
)
from efferva.runtime import CodexRuntime
from efferva.runtime_binary import locate_runtime_binary
from efferva.sandbox import create_sandbox_control_plane, register_sandbox_provider
from efferva.worker import RunWorker


@dataclass(slots=True)
class _RuntimeResources:
    repository: SystemRepository | None = None
    worker: RunWorker | None = None

    def require_repository(self) -> SystemRepository:
        if self.repository is None:
            raise RuntimeError("Efferva application has not started")
        return self.repository

    def worker_healthy(self) -> bool:
        return self.worker is not None and self.worker.healthy


class Efferva:
    """Product-facing facade for installing Efferva into an authenticated application."""

    def __init__(
        self,
        *,
        identity: IdentityResolver,
        settings: Settings | None = None,
    ) -> None:
        self.identity = identity
        self.settings = settings or get_settings()

    def install(self, app: FastAPI, *, prefix: str = "/agent") -> None:
        normalized_prefix = self._normalize_prefix(prefix)
        installed_prefixes = getattr(app.state, "_efferva_prefixes", set())
        if normalized_prefix in installed_prefixes:
            raise RuntimeError(f"Efferva is already installed at {normalized_prefix or '/'}")
        app.state._efferva_prefixes = {*installed_prefixes, normalized_prefix}

        resources = _RuntimeResources()
        settings = self.settings
        migrations_dir = files("efferva.migrations")
        migrations = [
            (path.name, path.read_text())
            for path in sorted(migrations_dir.iterdir(), key=lambda item: item.name)
            if path.name.endswith(".sql")
        ]

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            runtime_binary = locate_runtime_binary(settings.runtime_binary)
            database = Database(settings.database_url)
            runtime: CodexRuntime | None = None
            worker: RunWorker | None = None
            sandboxes = None
            await database.open()
            try:
                await database.migrate(migrations)
                repository = SystemRepository(database)
                runtime = CodexRuntime(
                    runtime_binary,
                    settings.database_url,
                    openai_base_url=settings.codex_openai_base_url,
                    model=settings.codex_model,
                )
                await runtime.start()
                sandboxes = create_sandbox_control_plane(settings, repository)
                await sandboxes.start()
                worker = RunWorker(settings, repository, runtime, sandboxes)
                resources.repository = repository
                resources.worker = worker
                await worker.start()
                yield
            finally:
                resources.worker = None
                resources.repository = None
                try:
                    if worker is not None:
                        await worker.close()
                    else:
                        if sandboxes is not None:
                            await sandboxes.close()
                        if runtime is not None:
                            await runtime.close()
                finally:
                    await database.close()

        router = APIRouter(lifespan=lifespan)
        router.include_router(
            create_api_router(
                identity=self.identity,
                system_repository=resources.require_repository,
                worker_healthy=resources.worker_healthy,
            )
        )

        static_dir = files("efferva").joinpath("static")
        require_principal = principal_dependency(self.identity)
        IndexPrincipal = Annotated[Principal, Depends(require_principal)]

        @router.get("/", include_in_schema=False)
        async def index(_: IndexPrincipal) -> FileResponse:
            return FileResponse(str(static_dir.joinpath("index.html")))

        @router.get("/static/{asset_name}", include_in_schema=False)
        async def static_asset(asset_name: str) -> FileResponse:
            if asset_name not in {"app.js", "style.css"}:
                raise HTTPException(status_code=404, detail="asset not found")
            return FileResponse(str(static_dir.joinpath(asset_name)))

        app.include_router(router, prefix=normalized_prefix)
        self._install_exception_handlers(app)

    def asgi_app(self) -> FastAPI:
        app = FastAPI(title="Efferva")
        self.install(app, prefix="")
        return app

    def app(self) -> FastAPI:
        """Compatibility alias for asgi_app()."""

        return self.asgi_app()

    @classmethod
    def register_sandbox_provider(cls, name: str, provider) -> None:
        register_sandbox_provider(name, provider)

    @staticmethod
    def _normalize_prefix(prefix: str) -> str:
        if prefix in {"", "/"}:
            return ""
        if not prefix.startswith("/"):
            raise ValueError("prefix must start with '/'")
        return prefix.rstrip("/")

    @staticmethod
    def _install_exception_handlers(app: FastAPI) -> None:
        async def not_found_handler(_, error: NotFoundError) -> JSONResponse:
            return JSONResponse(status_code=404, content={"detail": str(error)})

        async def conflict_handler(_, error: ConflictError) -> JSONResponse:
            return JSONResponse(status_code=409, content={"detail": str(error)})

        async def unauthenticated_handler(_, error: UnauthenticatedError) -> JSONResponse:
            detail = str(error) or "authentication required"
            return JSONResponse(status_code=401, content={"detail": detail})

        async def forbidden_handler(_, error: ForbiddenError) -> JSONResponse:
            return JSONResponse(status_code=403, content={"detail": str(error)})

        app.add_exception_handler(NotFoundError, not_found_handler)
        app.add_exception_handler(ConflictError, conflict_handler)
        app.add_exception_handler(UnauthenticatedError, unauthenticated_handler)
        app.add_exception_handler(ForbiddenError, forbidden_handler)
